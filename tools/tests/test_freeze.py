# -*- coding: utf-8 -*-
"""冻结名单的测试.

"冻结"的承诺有两条, 缺一不可:
    1. 一切下载逻辑都不再包含它(不刷新、不下载、不计入待补齐)
    2. 它的名单原样留存(下载状态.json / 已下载 / 跳过 一个字不动)

第 2 条特别重要 —— 用户最怕的就是"冻结之后名单被清了"。
"""

import json
import os
import unittest

from support import TempRootTest, folder, paths, read_state, write_state

from bbdown_kit import freeze, state as state_mod, tasks


class FreezeBase(TempRootTest):
    def setUp(self):
        super().setUp()
        self.up = folder("UP主下载", "123_测试UP")
        write_state(self.up, videos=[
            {"aid": "1", "bvid": "BV1", "title": "没下过的"},
            {"aid": "2", "bvid": "BV2", "title": "已下载"},
        ], record={"2": {"title": "已下载", "bvid": "BV2", "time": "x"}},
            skip={"9": {"title": "跳过的", "time": "x"}},
            meta={"最新aid": "1", "视频总数": 2, "列表完整": True,
                  "全量时间": "2026-10-01 00:00:00", "同步方式": "全量"})
        with open(os.path.join(self.up, "已下载_2.mp4"), "wb") as f:
            f.write(b"x")
        self.rel = os.path.join("UP主下载", "123_测试UP")


class TestFreezeLifecycle(FreezeBase):
    def test_freeze_then_unfreeze(self):
        self.assertFalse(freeze.is_frozen(self.rel))
        ok, _why = freeze.freeze(self.rel, "太费劲")
        self.assertTrue(ok)
        self.assertTrue(freeze.is_frozen(self.rel))
        # 冻结条目里记了时间和原因
        row = freeze.entries()[0]
        self.assertEqual(row["文件夹"], self.rel)
        self.assertEqual(row["原因"], "太费劲")
        self.assertTrue(row["冻结时间"])

        ok, _why = freeze.unfreeze(self.rel)
        self.assertTrue(ok)
        self.assertFalse(freeze.is_frozen(self.rel))
        self.assertEqual(freeze.entries(), [])

    def test_freeze_is_idempotent(self):
        freeze.freeze(self.rel)
        ok, why = freeze.freeze(self.rel)
        self.assertFalse(ok, "重复冻结要说不, 而不是悄悄再来一次")
        self.assertIn("已经在", why)

    def test_unfreeze_something_not_frozen(self):
        ok, why = freeze.unfreeze(self.rel)
        self.assertFalse(ok)
        self.assertIn("不在冻结名单", why)

    def test_freeze_refuses_a_folder_that_does_not_exist(self):
        """拼错名字要当场报错, 不能"冻结成功"了其实什么都没冻."""
        ok, why = freeze.freeze(os.path.join("UP主下载", "999_不存在"))
        self.assertFalse(ok)
        self.assertIn("找不到", why)
        self.assertFalse(freeze.frozen_set())

    def test_freeze_never_touches_the_state_file(self):
        """**最关键的一条**: 冻结/解冻都不能碰名单本身."""
        before = read_state(self.up)
        freeze.freeze(self.rel)
        after_freeze = read_state(self.up)
        freeze.unfreeze(self.rel)
        after_unfreeze = read_state(self.up)
        self.assertEqual(before, after_freeze)
        self.assertEqual(before, after_unfreeze)
        # 而且磁盘上的成片也还在
        self.assertTrue(os.path.exists(os.path.join(self.up, "已下载_2.mp4")))


class TestFreezeExcludesFromWork(FreezeBase):
    """冻结之后, 所有"要干活"的地方都不该再看到它."""

    def test_not_in_scan_all_folders(self):
        self.assertIn(self.rel, tasks.scan_all_folders())
        freeze.freeze(self.rel)
        self.assertNotIn(self.rel, tasks.scan_all_folders(),
                         "冻结的名单不该出现在『要干活』的列表里")
        # 但把它算进来时还看得到(报告要用)
        self.assertIn(self.rel, tasks.scan_all_folders(include_frozen=True))

    def test_not_in_build_all_tasks(self):
        """一键更新 / 定时任务走的就是这里 —— 冻结在这里生效."""
        up, coll, single = tasks.build_all_tasks()
        self.assertEqual([t[1] for t in up], [self.rel])
        freeze.freeze(self.rel)
        up, coll, single = tasks.build_all_tasks()
        self.assertEqual(up, [], "冻结之后不该再给它派任务")

    def test_frozen_folder_is_not_counted_as_pending(self):
        from bbdown_kit import analyze

        totals, _lines = analyze.status_report({})
        self.assertEqual(totals["pending"], 1)        # 只有 aid=1 没下过
        self.assertEqual(totals["frozen"], 0)

        freeze.freeze(self.rel)
        totals, lines = analyze.status_report({})
        self.assertEqual(totals["frozen"], 1)
        self.assertEqual(totals["pending"], 0,
                         "冻结的名单不该计入待补齐")
        self.assertEqual(totals["recorded"], 0,
                         "冻结的名单也不该计入已下载合计(它单列一行)")
        # 报告里要看得见它, 并说明怎么启用
        text = "\n".join(lines)
        self.assertIn("已冻结", text)
        self.assertIn("123_测试UP", text)
        self.assertIn("--unfreeze", text)

    def test_status_still_counts_everything_when_unfrozen(self):
        from bbdown_kit import analyze

        freeze.freeze(self.rel)
        freeze.unfreeze(self.rel)
        totals, _lines = analyze.status_report({})
        self.assertEqual(totals["frozen"], 0)
        self.assertEqual(totals["pending"], 1)


class TestFreezeMatchOne(FreezeBase):
    """三种写法都要能认出目标(日常真的会顺手敲)."""

    def test_by_uid(self):
        self.assertEqual(freeze.match_one("123"), self.rel)

    def test_by_folder_name(self):
        self.assertEqual(freeze.match_one("123_测试UP"), self.rel)

    def test_by_full_relative_path(self):
        self.assertEqual(freeze.match_one(self.rel), self.rel)

    def test_tolerates_quotes_and_forward_slashes(self):
        self.assertEqual(freeze.match_one('"123"'), self.rel)
        self.assertEqual(freeze.match_one("UP主下载/123_测试UP"), self.rel)

    def test_unknown_target(self):
        self.assertIsNone(freeze.match_one("999999"))
        self.assertIsNone(freeze.match_one(""))
        self.assertIsNone(freeze.match_one("随便什么"))


class TestFreezeStale(FreezeBase):
    """文件夹被改名/删掉之后, 冻结条目会失效 —— 要能发现并清理."""

    def test_stale_entry_is_detected(self):
        freeze.freeze(self.rel)
        # 手工把条目改成指向一个不存在的文件夹
        data = freeze.load()
        data["UP主下载\\999_早就没了"] = {"文件夹": "UP主下载\\999_早就没了"}
        freeze._save(data)
        stale = freeze.stale_entries()
        self.assertIn("UP主下载\\999_早就没了", stale)
        self.assertNotIn(self.rel, stale)

    def test_cleanup_stale(self):
        freeze.freeze(self.rel)
        data = freeze.load()
        data["UP主下载\\999_早就没了"] = {}
        freeze._save(data)
        removed = freeze.cleanup_stale()
        self.assertEqual(removed, ["UP主下载\\999_早就没了"])
        self.assertTrue(freeze.is_frozen(self.rel), "好条目不能被误删")

    def test_audit_reports_stale_entry(self):
        from bbdown_kit import analyze

        freeze.freeze(self.rel)
        data = freeze.load()
        data["UP主下载\\999_早就没了"] = {}
        freeze._save(data)
        text = "\n".join(analyze.audit_report({}))
        self.assertIn("冻结名单里", text)
        self.assertIn("999_早就没了", text)


class TestFreezeRobustness(FreezeBase):
    def test_missing_file_means_nothing_frozen(self):
        self.assertFalse(os.path.exists(paths.frozen_file()))
        self.assertEqual(freeze.frozen_set(), set())
        self.assertEqual(freeze.entries(), [])

    def test_broken_file_does_not_block_downloads(self):
        """冻结名单读坏了, 也绝不能因此拦住下载(宁可当成没冻结)."""
        os.makedirs(paths.log_root(), exist_ok=True)
        with open(paths.frozen_file(), "w", encoding="utf-8") as f:
            f.write("{ 这不是 JSON")
        self.assertEqual(freeze.frozen_set(), set())
        self.assertIn(self.rel, tasks.scan_all_folders())

    def test_handwritten_true_form_is_accepted(self):
        """手工写成 {"文件夹": true} 也认."""
        os.makedirs(paths.log_root(), exist_ok=True)
        with open(paths.frozen_file(), "w", encoding="utf-8") as f:
            json.dump({self.rel: True}, f, ensure_ascii=False)
        self.assertTrue(freeze.is_frozen(self.rel))
        self.assertEqual(freeze.names(), [self.rel])

    def test_freeze_does_not_affect_other_folders(self):
        other = folder("UP主下载", "456_另一个")
        write_state(other, videos=[{"aid": "5", "bvid": "", "title": "x"}])
        freeze.freeze(self.rel)
        others = os.path.join("UP主下载", "456_另一个")
        self.assertIn(others, tasks.scan_all_folders())
        self.assertNotIn(self.rel, tasks.scan_all_folders())


class TestFrozenFolderIsRefusedByDownload(TempRootTest):
    """兜底: 就算有人硬把冻结名单塞进下载流程, 也必须被拒绝."""

    def test_process_up_refuses_a_frozen_folder(self):
        up = folder("UP主下载", "123_测试UP")
        write_state(up, videos=[{"aid": "1", "bvid": "BV1", "title": "t"}])
        rel = os.path.join("UP主下载", "123_测试UP")
        freeze.freeze(rel)

        from bbdown_kit import download
        calls = []
        original = download.bilitools.sync_videos
        self.addCleanup(setattr, download.bilitools, "sync_videos", original)
        download.bilitools.sync_videos = lambda *a, **k: calls.append(a)
        stats = {"downloaded": 0, "failed": 0}
        # 传 None 当 session/key 也不会炸 —— 因为它在取列表之前就该返回
        download.process_up(None, None, up, "123", _args(), stats)
        self.assertEqual(calls, [], "冻结的名单不该去拉投稿列表")
        self.assertEqual(stats, {"downloaded": 0, "failed": 0})


def _args():
    from types import SimpleNamespace
    return SimpleNamespace(full=False, backfill=False, retry_skip=False,
                           full_days=7, limit=None, yes=True)


if __name__ == "__main__":
    unittest.main()
