# -*- coding: utf-8 -*-
"""下载编排的测试: 名单 -> 待下载 -> 落盘 -> 记账.

这里用的是假的 run_bbdown(直接造文件), 所以离线且不碰 BBDown.exe。
"""

import os
import re
import unittest
from types import SimpleNamespace
from unittest import mock

from support import (FakeSession, TempRootTest, folder, read_state, season_page,
                     touch, write_state)

from bbdown_kit import bilitools, download, tasks, util


def make_args(**over):
    args = dict(retry_skip=False, backfill=False, yes=True, limit=None,
                parallel=2, interval=0.0, fail_threshold=10, pause_seconds=0,
                extra_args=[], download_timeout=None, full=False, full_days=7)
    args.update(over)
    return SimpleNamespace(**args)


class DownloadTestBase(TempRootTest):
    def setUp(self):
        super().setUp()
        self.downloaded = []
        self.fail_aids = set()
        self.original = download.run_bbdown
        self.addCleanup(setattr, download, "run_bbdown", self.original)
        download.run_bbdown = self._fake_run_bbdown
        self.args = make_args()
        self.stats = {"downloaded": 0, "failed": 0}

    def _fake_run_bbdown(self, url, folder, extra_args, timeout=None, aid=None):
        aid = re.search(r"av(\d+)", url).group(1)
        self.downloaded.append(aid)
        if aid in self.fail_aids:
            return 1                       # 故意失败, 不产生文件
        # 像真的 BBDown 那样按「标题_aid」落盘(标题从状态文件里查) ——
        # 假下载要是随手造个和标题无关的名字, 就测不出"成功判定必须按
        # 视频自己对号"这条(整目录差分那条老路会被这种假名字喂饱)。
        title = "标题%s" % aid
        try:
            for v in read_state(folder).get("投稿列表", []):
                if str(v.get("aid")) == aid:
                    title = util.sanitize_name(v.get("title") or title)
                    break
        except Exception:
            pass
        with open(os.path.join(folder, "%s_%s.mp4" % (title, aid)), "wb") as f:
            f.write(b"x")
        return 0

    def spec(self, videos=None, name="某某合集", sid="1000001"):
        return {
            "类型": "合集", "mid": "123456781", "id": sid, "名称": name,
            "视频列表": videos or [
                {"aid": "1", "bvid": "BV1", "title": "标题1"},
                {"aid": "2", "bvid": "BV2", "title": "标题2"},
            ],
        }


class TestCollectionFlow(DownloadTestBase):
    """整套走一遍: 合集 -> 建目录 -> 写状态 -> 下载 -> 第二次不重复下."""

    def test_first_run_downloads_and_records(self):
        download.process_collection(None, self.spec(), self.args, self.stats)
        coll = folder("合集下载", "某某合集_1000001")
        data = read_state(coll)
        self.assertEqual(sorted(data["已下载"]), ["1", "2"])
        self.assertEqual(data["同步信息"]["类型"], "合集")
        self.assertEqual(data["同步信息"]["mid"], "123456781")
        self.assertEqual(data["同步信息"]["编号"], "1000001")
        self.assertEqual(len(data["投稿列表"]), 2)
        self.assertEqual(sorted(self.downloaded), ["1", "2"])
        self.assertEqual(self.stats["downloaded"], 2)

    def test_second_run_does_not_download_again(self):
        download.process_collection(None, self.spec(), self.args, self.stats)
        download.process_collection(None, self.spec(), self.args, self.stats)
        self.assertEqual(sorted(self.downloaded), ["1", "2"])   # 只下过一次
        self.assertEqual(self.stats["downloaded"], 2)

    def test_new_video_in_collection_is_picked_up(self):
        download.process_collection(None, self.spec(), self.args, self.stats)
        later = self.spec()
        later["视频列表"].append({"aid": "3", "bvid": "BV3", "title": "标题3"})
        download.process_collection(None, later, self.args, self.stats)
        self.assertEqual(sorted(self.downloaded), ["1", "2", "3"])
        self.assertEqual(self.stats["downloaded"], 3)

    def test_limit_zero_only_refreshes_list(self):
        self.args.limit = 0
        download.process_collection(None, self.spec(), self.args, self.stats)
        coll = folder("合集下载", "某某合集_1000001")
        data = read_state(coll)
        self.assertEqual(len(data["投稿列表"]), 2)
        self.assertEqual(data["已下载"], {})
        self.assertEqual(self.downloaded, [])

    def test_completion_lines_carry_elapsed_time(self):
        """每条的完成/失败日志都要带"用时", 不然看不出是慢还是卡."""
        messages = []
        original = download.log
        self.addCleanup(setattr, download, "log", original)
        download.log = lambda msg: messages.append(msg)
        download.process_collection(None, self.spec(), self.args, self.stats)
        done = [x for x in messages if "完成 ✓" in x]
        self.assertEqual(len(done), 2)
        for line in done:
            self.assertIn("用时", line)
            self.assertIn("平均", line)

    def test_guard_path_fetches_list_from_state(self):
        """守护走的路: 状态里只有 mid+编号, 列表每次现拉, 已下的不重复下."""
        coll = folder("合集下载", "某某合集_1000001")
        spec = {"类型": "合集", "mid": "123456781", "id": "1000001",
                "名称": "某某合集"}
        page = [("1", "标题1"), ("2", "标题2")]
        session = FakeSession([("seasons_archives_list", season_page(page, 2))])
        download.process_collection(session, spec, self.args, self.stats,
                                    folder=coll)
        self.assertEqual(sorted(read_state(coll)["已下载"]), ["1", "2"])
        # 第二轮: 状态里读回 mid/编号(守护就是这么重建任务的)
        again = tasks.collection_spec_from_state(
            os.path.join(tasks.COLL, "某某合集_1000001"))
        self.assertIsNone(again.get("视频列表"))
        session2 = FakeSession([("seasons_archives_list", season_page(page, 2))])
        download.process_collection(session2, again, self.args, self.stats,
                                    folder=coll)
        self.assertEqual(sorted(self.downloaded), ["1", "2"])   # 没有重复下载


class TestSkipRules(DownloadTestBase):
    """「跳过」是唯一会永久丢东西的地方, 规则要写死且可测."""

    def test_two_failures_move_to_skip(self):
        """跨两次运行各失败一次 -> 第 2 次才进「跳过」(永久不再重试)."""
        self.fail_aids = {"1", "2"}
        # 第一次运行: 只进「失败」, 还会重试
        download.process_collection(None, self.spec(), self.args, self.stats)
        coll = folder("合集下载", "某某合集_1000001")
        data = read_state(coll)
        self.assertEqual(data["跳过"], {})
        self.assertEqual(data["失败"]["1"]["count"], 1)
        # 第二次运行: 再失败一次, 累计 2 次 -> 进「跳过」
        self.downloaded.clear()
        download.process_collection(None, self.spec(), self.args, self.stats)
        data = read_state(coll)
        self.assertIn("1", data["跳过"])
        self.assertIn("2", data["跳过"])
        self.assertNotIn("1", data["失败"])
        self.assertNotIn("1", data["已下载"])
        self.assertEqual(sorted(self.downloaded), ["1", "2"])   # 两次都试过了

    def test_skipped_video_is_not_downloaded_again(self):
        """进了「跳过」的, 之后每次运行都不能再试(否则永远耗在它身上)."""
        self.fail_aids = {"1"}
        videos = [{"aid": "1", "bvid": "BV1", "title": "标题1"}]
        spec = self.spec(videos=videos)
        coll = folder("合集下载", "某某合集_1000001")
        write_state(coll, videos=videos,
                    skip={"1": {"title": "标题1", "time": "x"}})
        download.process_collection(None, spec, self.args, self.stats, folder=coll)
        self.assertEqual(self.downloaded, [])
        self.assertIn("1", read_state(coll)["跳过"])

    def test_first_failure_goes_to_failed_not_skip(self):
        """只失败一次不能进「跳过」, 否则限流一次就丢一批视频."""
        self.fail_aids = {"1", "2"}
        videos = [{"aid": "1", "bvid": "BV1", "title": "标题1"}]
        spec = self.spec(videos=videos)
        download.process_collection(None, spec, self.args, self.stats)
        data = read_state(folder("合集下载", "某某合集_1000001"))
        self.assertIn("1", data["失败"])
        self.assertNotIn("1", data["跳过"])
        self.assertEqual(data["失败"]["1"]["count"], 1)

    def test_retry_skip_requeues(self):
        coll = folder("合集下载", "某某合集_1000001")
        write_state(coll, videos=[{"aid": "1", "bvid": "BV1", "title": "标题1"}],
                    skip={"1": {"title": "标题1", "time": "x"}})
        spec = self.spec(videos=[{"aid": "1", "bvid": "BV1", "title": "标题1"}])
        self.args.retry_skip = True
        download.process_collection(None, spec, self.args, self.stats,
                                    folder=coll)
        data = read_state(coll)
        self.assertNotIn("1", data["跳过"])
        self.assertIn("1", data["已下载"])
        self.assertEqual(self.downloaded, ["1"])

    def test_retry_skip_drops_stale_entries(self):
        """「跳过」里和「已下载」重复的条目要顺手清掉."""
        coll = folder("合集下载", "某某合集_1000001")
        write_state(coll, videos=[{"aid": "1", "bvid": "BV1", "title": "标题1"}],
                    record={"1": {"title": "标题1"}},
                    skip={"1": {"title": "标题1", "time": "x"},
                          "99": {"title": "UP删了的", "time": "x"}})
        spec = self.spec(videos=[{"aid": "1", "bvid": "BV1", "title": "标题1"}])
        self.args.retry_skip = True
        download.process_collection(None, spec, self.args, self.stats,
                                    folder=coll)
        data = read_state(coll)
        self.assertNotIn("1", data["跳过"])
        self.assertIn("99", data["跳过"])   # 不在名单里的原样保留


class TestBackfillAndReconcile(DownloadTestBase):
    def test_backfill_removes_records_without_files(self):
        coll = folder("合集下载", "某某合集_1000001")
        write_state(coll, videos=[{"aid": "1", "bvid": "BV1", "title": "标题1"}],
                    record={"1": {"title": "标题1", "time": "x"}})
        spec = self.spec(videos=[{"aid": "1", "bvid": "BV1", "title": "标题1"}])
        self.args.backfill = True
        download.process_collection(None, spec, self.args, self.stats,
                                    folder=coll)
        self.assertEqual(self.downloaded, ["1"])          # 重新下了
        self.assertIn("1", read_state(coll)["已下载"])

    def test_local_file_is_recorded_without_downloading(self):
        """本地已有同名文件的, 直接视为已下载, 不浪费一次下载."""
        coll = folder("合集下载", "某某合集_1000001")
        touch(coll, "标题1_1.mp4")
        download.process_collection(None, self.spec(), self.args, self.stats)
        data = read_state(coll)
        self.assertIn("1", data["已下载"])
        self.assertEqual(data["已下载"]["1"]["time"], "已按本地文件补记")
        self.assertEqual(self.downloaded, ["2"])          # 只下了缺的那个


class TestSingleVideoFlow(DownloadTestBase):
    def test_register_then_download(self):
        info = {"aid": "555", "bvid": "BV555", "title": "单个视频"}
        st = download.register_single_video(info)
        single = folder("单视频下载")
        self.assertEqual([v["aid"] for v in read_state(single)["投稿列表"]],
                         ["555"])
        self.assertTrue(download.download_single_now(st, info, self.args,
                                                    self.stats))
        data = read_state(single)
        self.assertIn("555", data["已下载"])
        self.assertEqual(self.stats["downloaded"], 1)

    def test_failed_single_video_is_remembered(self):
        """菜单按 S 下的视频: 失败也要登记下来, 下次守护能补."""
        info = {"aid": "555", "bvid": "BV555", "title": "下不下来的视频"}
        st = download.register_single_video(info)
        self.fail_aids = {"555"}
        self.assertFalse(download.download_single_now(st, info, self.args,
                                                     self.stats))
        data = read_state(folder("单视频下载"))
        self.assertEqual([v["aid"] for v in data["投稿列表"]], ["555"])
        self.assertIn("555", data["失败"])
        self.assertEqual(self.stats["failed"], 1)

    def test_process_single_folder_downloads_and_retries(self):
        """守护/一键更新走的路: 把单视频清单里没下成的补上."""
        single = folder("单视频下载")
        write_state(single, [{"aid": "7", "bvid": "BV7", "title": "补下这个"}])
        download.process_single_folder(self.args, self.stats, folder=single)
        self.assertEqual(len(self.downloaded), 1)
        self.assertEqual(sorted(read_state(single)["已下载"]), ["7"])
        self.assertEqual(self.stats["downloaded"], 1)

    def test_process_single_folder_skips_empty(self):
        single = folder("单视频下载")
        os.makedirs(single, exist_ok=True)
        download.process_single_folder(self.args, self.stats, folder=single)
        self.assertEqual(self.downloaded, [])


class TestProcessUp(DownloadTestBase):
    """按UP主: 同步列表 + 下载."""

    def test_downloads_new_and_keeps_existing(self):
        up = folder("UP主下载", "123_测试UP")
        write_state(up, videos=[{"aid": "1", "bvid": "BV1", "title": "老的"}],
                    record={"1": {"title": "老的"}},
                    meta={"最新aid": "1", "视频总数": 1, "列表完整": True,
                          "全量时间": "2026-09-26 17:53:31"})
        touch(up, "老的_1.mp4")
        synced = [{"aid": "2", "bvid": "BV2", "title": "新的"},
                  {"aid": "1", "bvid": "BV1", "title": "老的"}]
        with mock.patch.object(bilitools, "sync_videos") as fake:
            fake.return_value = bilitools.SyncResult(synced, {
                "最新aid": "2", "视频总数": 2, "列表完整": True,
                "全量时间": "2026-09-29 00:00:00", "同步方式": "增量"}, "incremental")
            download.process_up(None, None, up, "123", self.args, self.stats)
        self.assertEqual(self.downloaded, ["2"])        # 只下新的
        self.assertEqual(sorted(read_state(up)["已下载"]), ["1", "2"])

    def test_sync_failure_does_not_break_run(self):
        up = folder("UP主下载", "123_测试UP")
        os.makedirs(up, exist_ok=True)
        with mock.patch.object(bilitools, "sync_videos",
                               side_effect=RuntimeError("接口炸了")):
            download.process_up(None, None, up, "123", self.args, self.stats)
        self.assertEqual(self.stats["failed"], 1)
        self.assertEqual(self.downloaded, [])


class TestThrottle(unittest.TestCase):
    """自适应降速的计数规则."""

    def setUp(self):
        from bbdown_kit import lockstep
        self.lockstep = lockstep

    def test_backs_off_after_threshold(self):
        throttle = self.lockstep.Throttle(3, 2.0, 3, 60)
        self.assertFalse(throttle.on_failure())
        self.assertFalse(throttle.on_failure())
        self.assertTrue(throttle.on_failure())        # 第 3 个连续失败 -> 降速
        self.assertEqual(throttle.current_parallel, 1)
        self.assertGreaterEqual(throttle.interval, 5.0)
        self.assertTrue(throttle.cooling_down())

    def test_recovers_after_five_successes(self):
        throttle = self.lockstep.Throttle(3, 2.0, 3, 0)
        for _ in range(3):
            throttle.on_failure()
        throttle.cooldown_until = 0
        for _ in range(4):
            self.assertFalse(throttle.on_success())
        self.assertTrue(throttle.on_success())        # 第 5 个成功 -> 恢复
        self.assertEqual(throttle.current_parallel, 3)
        self.assertEqual(throttle.interval, 2.0)

    def test_run_batch_counts_results(self):
        from bbdown_kit import lockstep
        throttle = lockstep.Throttle(2, 0.0, 99, 0)
        videos = [{"aid": str(i), "title": "t%d" % i} for i in range(6)]

        def run_one(video):
            return video["aid"]

        def on_result(video, result, done, elapsed):
            return int(video["aid"]) % 2 == 0

        ok, failed = lockstep.run_batch(videos, run_one, on_result, throttle)
        self.assertEqual((ok, failed), (3, 3))

    def test_run_batch_treats_exception_as_failure(self):
        from bbdown_kit import lockstep
        throttle = lockstep.Throttle(1, 0.0, 99, 0)

        def run_one(video):
            raise RuntimeError("炸了")

        seen = []

        def on_result(video, result, done, elapsed):
            seen.append(result)
            return False

        ok, failed = lockstep.run_batch([{"aid": "1", "title": "t"}], run_one,
                                        on_result, throttle)
        self.assertEqual((ok, failed), (0, 1))
        self.assertEqual(seen, [None])


class TestVideoUnplayable(unittest.TestCase):
    """充电/付费专属的判定(这类永远下不了, 不该算成限流)."""

    def setUp(self):
        bilitools.clear_unplayable_cache()

    def tearDown(self):
        bilitools.clear_unplayable_cache()

    def _check(self, payload):
        session = mock.Mock()
        session.get.return_value.json.return_value = payload
        with mock.patch.object(bilitools, "api_session", lambda: session):
            return bilitools.video_unplayable("1")

    def test_exclusive_and_cannot_play(self):
        self.assertTrue(self._check({"data": {"is_upower_exclusive": True,
                                              "is_upower_play": False}}))

    def test_exclusive_but_can_play(self):
        self.assertFalse(self._check({"data": {"is_upower_exclusive": True,
                                               "is_upower_play": True}}))

    def test_ugc_pay_preview(self):
        self.assertTrue(self._check({"data": {"is_ugc_pay_preview": True}}))

    def test_normal_video(self):
        self.assertFalse(self._check({"data": {"is_upower_exclusive": False}}))

    def test_network_error_is_not_unplayable(self):
        session = mock.Mock()
        session.get.side_effect = RuntimeError("网络炸了")
        with mock.patch.object(bilitools, "api_session", lambda: session):
            self.assertFalse(bilitools.video_unplayable("1"))


if __name__ == "__main__":
    unittest.main()
