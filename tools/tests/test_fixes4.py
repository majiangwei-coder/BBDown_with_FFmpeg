# -*- coding: utf-8 -*-
"""第 4 批修复的回归测试(交叉复审查出来的残留缺陷).

每一条对应一个真实会出错的场景, 注释里写清"不修会怎样":
  · 「猫咪」把「猫咪 日常_222.mp4」认成自己的 -> 永远不下;
  · 超时被掐掉的下载留下半成品 -> 被当成已下载, 那个坏文件再也没人管;
  · 名录里"失败过一次"的视频被算成"永久失败" -> 守护宣布全部补齐并退出;
  · 降速只生效一次 / 反而提速;
  · 状态文件被改成 [] 之后被静默当成空状态盖掉;
  · 接口返回非对象时 AttributeError 把守护带崩。
"""

import importlib.util
import os
import time
import unittest
from unittest import mock

from support import REAL_ROOT, TempRootTest, paths, touch

from bbdown_kit import analyze, bilitools, download, lockstep, media
from bbdown_kit import state as state_mod


def load_by_path(name, rel):
    """按路径加载(有的脚本文件名带横线/中文, 不能直接 import)."""
    path = os.path.join(REAL_ROOT, rel)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# =============== 甲: 静默少下 / 把半成品当成功 ===============

class TestPrefixNeverStealsAnotherVideo(TempRootTest):
    """标题互为前缀的两个视频, 不能互相认领文件."""

    def test_shorter_title_does_not_claim_the_longer_ones_file(self):
        """「猫咪」不能因为「猫咪 日常_222.mp4」在, 就以为自己下过了."""
        folder = self.up_folder("1_UP")
        touch(folder, "猫咪 日常_222.mp4")
        index = media.MediaIndex(folder)
        self.assertFalse(index.has("456", "猫咪", frozenset()))
        # 那个文件真正的主人认得出来
        self.assertTrue(index.has("222", "猫咪 日常", frozenset()))

    def test_own_aid_in_an_old_style_name_still_counts(self):
        """老写法「标题 456.mp4」(空格分隔 + 自己的 aid)还是要认."""
        folder = self.up_folder("1_UP")
        touch(folder, "猫咪 456.mp4")
        self.assertTrue(media.MediaIndex(folder).has("456", "猫咪", frozenset()))

    def test_another_aid_with_a_space_is_not_mine(self):
        folder = self.up_folder("1_UP")
        touch(folder, "猫咪 123.mp4")
        self.assertFalse(media.MediaIndex(folder).has("456", "猫咪", frozenset()))

    def test_old_name_without_any_aid_is_still_lenient(self):
        """根本没有 aid 后缀的老文件只能靠标题认, 保持宽松."""
        folder = self.up_folder("1_UP")
        touch(folder, "猫咪 上集.mp4")
        self.assertTrue(media.MediaIndex(folder).has("456", "猫咪", frozenset()))

    def test_multi_part_folder_of_another_video_is_not_mine(self):
        folder = self.up_folder("1_UP")
        touch(folder, os.path.join("猫咪 日常_222", "[P01]上.mp4"))
        self.assertFalse(media.MediaIndex(folder).has("456", "猫咪", frozenset()))

    def test_single_char_title_still_works(self):
        folder = self.up_folder("1_UP")
        touch(folder, "选_9.mp4")
        self.assertTrue(media.MediaIndex(folder).has("9", "选", frozenset()))


class TestTimedOutDownloadIsFailure(TempRootTest):
    """rc = -9 是"我们把它掐掉的", 那一次留下的成片一定是半成品."""

    def _run(self, rc, make_file=True):
        folder = self.up_folder("1_UP")
        os.makedirs(folder, exist_ok=True)
        video = {"aid": "456", "title": "猫咪"}

        def fake_run(url, workdir, extra, timeout=None, aid=None):
            if make_file:
                touch(folder, "猫咪_456.mp4", b"half-written")
            return rc

        with mock.patch.object(download, "run_bbdown", fake_run):
            return folder, download.download_one(folder, video, [], set(),
                                                 timeout=1)

    def test_killed_download_is_not_success_and_partial_is_dropped(self):
        folder, (_v, ok, rc, new_file, _t) = self._run(-9)
        self.assertEqual(rc, -9)
        self.assertFalse(ok, "超时被掐掉的下绝不能算成功")
        self.assertFalse(new_file)
        self.assertFalse(os.path.exists(os.path.join(folder, "猫咪_456.mp4")),
                         "半成品要删掉, 否则下次 BBDown 会以为已经下好了")

    def test_normal_success_still_counts(self):
        folder, (_v, ok, rc, new_file, _t) = self._run(0)
        self.assertTrue(ok)
        self.assertTrue(new_file)
        self.assertTrue(os.path.exists(os.path.join(folder, "猫咪_456.mp4")))

    def test_failure_without_a_file_removes_nothing(self):
        folder, (_v, ok, _rc, _nf, _t) = self._run(1, make_file=False)
        self.assertFalse(ok)
        self.assertEqual(os.listdir(folder), [])


class TestPendingIgnoresOnlyPermanentQuarantine(TempRootTest):
    """只是失败过一次(不是永久失败)的视频, 仍然算"还缺"."""

    def _report(self, quarantined):
        folder = self.up_folder("1_UP")
        from support import write_state
        write_state(folder, videos=[{"aid": "456", "title": "猫咪"}])
        return [aid for r in analyze.all_reports(quarantined)
                for aid in r.pending]

    def test_plain_quarantine_entry_is_still_pending(self):
        self.assertEqual(self._report({"456": {"fails": 1}}), ["456"])

    def test_permanent_entry_is_not_pending(self):
        self.assertEqual(
            self._report({"456": {"fails": 9, "permanent": True}}), [])

    def test_quarantine_as_a_plain_set_still_works(self):
        self.assertEqual(self._report(frozenset(["456"])), [])

    def test_broken_quarantine_entry_does_not_crash(self):
        self.assertEqual(self._report({"456": "手册里改坏的字符串"}), ["456"])


# =============== 乙: 守护卡死 / 降速失灵 ===============

class TestThrottleKeepsWorking(unittest.TestCase):
    """降速不能只生效一次(以前 cooldown_until 一旦设过就再没人清零)."""

    def test_slowdown_fires_again_after_the_cooldown(self):
        th = lockstep.Throttle(parallel=3, interval=1.0, fail_threshold=2,
                               pause_seconds=10)
        th.on_failure()
        self.assertTrue(th.on_failure())
        self.assertEqual(th.backoff_level, 1)

        th.cooldown_until = time.time() - 1          # 冷却时间过去了
        self.assertTrue(th.on_failure(), "冷却结束后再连续失败, 应该再降速")
        self.assertEqual(th.backoff_level, 2)

    def test_while_cooling_down_it_does_not_trigger_again(self):
        th = lockstep.Throttle(3, 1.0, 2, 600)
        th.on_failure()
        self.assertTrue(th.on_failure())
        self.assertFalse(th.on_failure(), "冷却期间不该反复触发")


class TestSpeedLevelsNeverSpeedUp(unittest.TestCase):
    """限流后只能更慢 —— 以前会把"并发 1 / 间隔 10 秒"变成"并发 2 / 间隔 3 秒"."""

    @classmethod
    def setUpClass(cls):
        cls.guard = load_by_path("guard_mod", os.path.join("tools", "下载守护.py"))

    def test_a_conservative_setting_never_gets_more_aggressive(self):
        levels = self.guard.speed_levels(1, 10.0)
        for parallel, interval in levels:
            self.assertLessEqual(parallel, 1)
            self.assertGreaterEqual(interval, 10.0)
        for before, after in zip(levels, levels[1:]):
            self.assertLessEqual(after[0], before[0])
            self.assertGreaterEqual(after[1], before[1])
        self.assertGreater(levels[1][1], levels[0][1], "至少间隔要真的拉长")

    def test_default_setting_behaves_as_before(self):
        self.assertEqual(self.guard.speed_levels(2, 1.0),
                         [(2, 1.0), (2, 3.0), (1, 5.0), (1, 8.0)])


# =============== 丙: 坏文件 / 怪响应别崩 ===============

class TestWrongShapedStateFileIsKept(TempRootTest):
    """合法 JSON 但不是对象, 也要留证, 不能被当成空状态盖掉."""

    def test_json_array_is_quarantined(self):
        folder = self.up_folder("1_UP")
        os.makedirs(folder, exist_ok=True)
        with open(paths.state_file(folder), "w", encoding="utf-8") as f:
            f.write("[]")
        st = state_mod.State.load(folder)
        self.assertEqual(st.videos, [])
        kept = [n for n in os.listdir(folder) if ".bad-" in n]
        self.assertEqual(len(kept), 1, "坏文件必须改名留证")
        self.assertFalse(os.path.exists(paths.state_file(folder)))

    def test_readonly_load_touches_nothing(self):
        folder = self.up_folder("1_UP")
        os.makedirs(folder, exist_ok=True)
        with open(paths.state_file(folder), "w", encoding="utf-8") as f:
            f.write("null")
        state_mod.State.load(folder, readonly=True)
        self.assertTrue(os.path.exists(paths.state_file(folder)))
        self.assertEqual([n for n in os.listdir(folder) if ".bad-" in n], [])


class TestBadTypesInHandEditedState(TempRootTest):

    def test_pending_count_survives_a_string_in_the_list(self):
        folder = self.up_folder("1_UP")
        os.makedirs(folder, exist_ok=True)
        with open(paths.state_file(folder), "w", encoding="utf-8") as f:
            f.write('{"投稿列表": ["av123", {"aid": "456"}, 7]}')
        self.assertEqual(state_mod.pending_count(folder), 1)

    def test_record_round_failures_survives_a_broken_entry(self):
        from support import write_state
        from bbdown_kit import maintenance
        folder = self.up_folder("1_UP")
        write_state(folder, videos=[{"aid": "456", "title": "猫咪"}],
                    failed={"456": {"time": "2026-10-01 00:00:00",
                                    "title": "猫咪", "count": None}})
        quarantined = {}
        maintenance.record_round_failures("2026-09-30 00:00:00", quarantined)
        self.assertEqual(quarantined["456"]["fails"], 1)


class TestApiDefensive(unittest.TestCase):

    class _Session(object):
        def __init__(self, payload):
            self.payload = payload

        def get(self, *a, **kw):
            payload = self.payload

            class _Resp(object):
                def json(self):
                    return payload
            return _Resp()

    def test_check_login_survives_a_non_object_body(self):
        for payload in (["not", "an", "object"], "a string", 7, None):
            self.assertFalse(bilitools.check_login(self._Session(payload)))

    def test_probe_login_survives_a_non_object_body(self):
        conclusion, text = bilitools.probe_login()  # 没有 requests 时会走别的路
        self.assertIn(conclusion, ("network", "ok", "dead", "ratelimited"))

    def test_mixin_key_survives_null_urls(self):
        session = self._Session({"code": 0,
                                 "data": {"wbi_img": {"img_url": None,
                                                      "sub_url": None}}})
        self.assertIsNone(bilitools.mixin_key(session))

    def test_prefix_compare_survives_a_missing_aid(self):
        self.assertFalse(bilitools.page_matches_prefix([1, 2, 3], 1,
                                                       [{"title": "x"}]))

    def test_prefix_compare_still_matches_normally(self):
        stored = [{"aid": 3}, {"aid": 4}]
        self.assertTrue(bilitools.page_matches_prefix([9, 3, 4], 1, stored))


class TestManagerArgMerging(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mgr = load_by_path("mgr_mod",
                               os.path.join("tools", "BBDown-manager.py"))

    def test_option_looking_value_is_merged(self):
        merged = self.mgr._merge_value_args(["--bbdown-args", "-h"])
        self.assertEqual(merged, ["--bbdown-args=-h"])
        # 合写之后, 单独的 -h 不该再被当成"看本程序帮助"
        self.assertNotIn("-h", merged)

    def test_plain_value_is_merged_too(self):
        merged = self.mgr._merge_value_args(["--bbdown-args", "--a --b",
                                             "--all"])
        self.assertEqual(merged, ["--bbdown-args=--a --b", "--all"])

    def test_a_bare_h_is_still_help(self):
        self.assertEqual(self.mgr._merge_value_args(["-h"]), ["-h"])

    def test_value_with_equals_is_left_alone(self):
        self.assertEqual(self.mgr._merge_value_args(["--bbdown-args=-x"]),
                         ["--bbdown-args=-x"])


if __name__ == "__main__":
    unittest.main()
