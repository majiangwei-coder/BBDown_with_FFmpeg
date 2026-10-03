# -*- coding: utf-8 -*-
"""下载状态.json 与本地文件判断的测试.

这两块是"会不会丢东西"的关键: 状态文件写错 = 视频被漏掉或重复下载;
判重写错 = 已下载的被重下, 或没下载的永远下不了。
"""

import json
import os
import tempfile
import unittest

from support import (TempRootTest, folder, read_state, rel, touch, write_state)

from bbdown_kit import media, paths, state as state_mod, tasks
from bbdown_kit.state import State


class TestStateFile(TempRootTest):
    """状态文件的读写契约."""

    def test_roundtrip_keeps_all_five_sections(self):
        up = folder("UP主下载", "123_测试UP")
        write_state(up,
                    videos=[{"aid": "1", "bvid": "BV1", "title": "视频1"}],
                    record={"1": {"title": "视频1", "bvid": "BV1", "time": "x"}},
                    skip={"2": {"title": "充电的", "time": "y"}},
                    failed={"3": {"title": "失败的", "count": 1, "time": "z"}},
                    meta={"最新aid": "1", "视频总数": 1})
        st = State.load(up)
        self.assertEqual([v["aid"] for v in st.videos], ["1"])
        self.assertEqual(list(st.record), ["1"])
        self.assertEqual(list(st.skip), ["2"])
        self.assertEqual(list(st.failed), ["3"])
        self.assertEqual(st.meta["视频总数"], 1)
        # 磁盘上的键名必须是中文的那五个(数据契约, 改了旧文件就认不出来)
        raw = read_state(up)
        self.assertEqual(sorted(raw), ["同步信息", "失败", "已下载", "投稿列表", "跳过"])

    def test_aid_must_stay_a_string(self):
        """aid 一旦变成 int, 就和「已下载」的键对不上 -> 全部重下一遍."""
        up = folder("UP主下载", "123_测试UP")
        write_state(up, videos=[{"aid": "100000000000004", "bvid": "BV1",
                                 "title": "大号"}],
                    record={"100000000000004": {"title": "大号", "time": "x"}})
        st = State.load(up)
        self.assertIsInstance(st.videos[0]["aid"], str)
        self.assertIn(st.videos[0]["aid"], st.record)

    def test_save_is_atomic_and_leaves_no_tmp(self):
        up = folder("UP主下载", "123_测试UP")
        st = write_state(up, videos=[{"aid": "1", "bvid": "", "title": "t"}])
        st.save()
        self.assertFalse(os.path.exists(paths.state_file(up) + ".tmp"))

    def test_broken_file_does_not_crash_load(self):
        up = folder("UP主下载", "123_测试UP")
        os.makedirs(up, exist_ok=True)
        with open(paths.state_file(up), "w", encoding="utf-8") as f:
            f.write("{ 这不是 JSON")
        st = State.load(up)              # 不该抛异常
        self.assertEqual(st.videos, [])
        self.assertEqual(st.record, {})

    def test_meta_forms_up_and_collection(self):
        up = folder("UP主下载", "123_测试UP")
        write_state(up, videos=[{"aid": "1", "bvid": "", "title": "t"}],
                    meta={"最新aid": "1", "视频总数": 5, "列表完整": True,
                          "全量时间": "2026-09-26 17:53:31", "同步方式": "全量"})
        st = State.load(up)
        self.assertEqual(st.api_count(), 5)
        self.assertTrue(st.is_list_complete())
        self.assertIsNotNone(st.full_time())
        self.assertIsNone(st.collection_spec())

        coll = folder("合集下载", "某某合集_1000001")
        write_state(coll, videos=[{"aid": "2", "bvid": "", "title": "回放"}],
                    meta={"类型": "合集", "mid": "123456781", "编号": "1000001",
                          "名称": "某某合集", "视频总数": 1})
        spec = State.load(coll).collection_spec()
        self.assertEqual(spec["类型"], "合集")
        self.assertEqual(spec["mid"], "123456781")
        self.assertEqual(spec["id"], "1000001")

    def test_state_without_collection_info_is_refused(self):
        """老的/手写的状态文件没有合集信息时, 不能瞎猜, 要返回 None."""
        coll = folder("合集下载", "手工放进来的")
        write_state(coll, videos=[{"aid": "1", "bvid": "", "title": "x"}])
        self.assertIsNone(State.load(coll).collection_spec())

    def test_duplicate_titles_detected(self):
        up = folder("UP主下载", "123_测试UP")
        write_state(up, videos=[
            {"aid": "1", "bvid": "", "title": "同一个标题"},
            {"aid": "2", "bvid": "", "title": "同一个标题"},
            {"aid": "3", "bvid": "", "title": "独一无二"},
        ])
        st = State.load(up)
        self.assertEqual(st.duplicate_titles(), {"同一个标题"})


class TestStateChanges(TempRootTest):
    """状态的三节怎么变(下成功/失败/跳过)."""

    def make(self):
        up = folder("UP主下载", "123_测试UP")
        return write_state(up, videos=[
            {"aid": "1", "bvid": "BV1", "title": "没下过的"},
            {"aid": "2", "bvid": "BV2", "title": "要失败的"},
            {"aid": "3", "bvid": "BV3", "title": "充电专属"},
            {"aid": "4", "bvid": "BV4", "title": "已下载"},
        ], record={"4": {"title": "已下载", "bvid": "BV4", "time": "x"}})

    def test_pending_excludes_recorded_and_skipped(self):
        st = self.make()
        self.assertEqual([v["aid"] for v in st.pending()], ["1", "2", "3"])
        st.force_skip("3", "充电专属", state_mod.TIME_CHARGING)
        self.assertEqual([v["aid"] for v in st.pending()], ["1", "2"])

    def test_mark_done_clears_fail_and_skip(self):
        st = self.make()
        st.mark_failed("2", "要失败的")
        st.force_skip("2", "要失败的", "x")
        st.mark_done("2", "要失败的", "BV2")
        self.assertIn("2", st.record)
        self.assertNotIn("2", st.failed)
        self.assertNotIn("2", st.skip)

    def test_two_failures_move_to_skip(self):
        """连续失败 2 次才进「跳过」——这是唯一会永久丢东西的动作."""
        st = self.make()
        self.assertEqual(st.mark_failed("2", "要失败的"), 1)
        self.assertIn("2", st.failed)
        self.assertEqual(st.mark_failed("2", "要失败的"), 2)
        st.move_failed_to_skip("2")
        self.assertIn("2", st.skip)
        self.assertNotIn("2", st.failed)

    def test_build_meta_for_up_and_collection(self):
        st = self.make()
        meta = st.build_up_meta(st.videos, 4, complete=True)
        self.assertEqual(meta["最新aid"], "1")
        self.assertEqual(meta["视频总数"], 4)
        self.assertTrue(meta["列表完整"])
        coll_meta = st.build_collection_meta(
            {"类型": "系列", "mid": "9", "id": "77", "名称": "某某"}, 3)
        self.assertEqual(coll_meta["类型"], "系列")
        self.assertEqual(coll_meta["编号"], "77")
        self.assertEqual(coll_meta["同步方式"], "系列接口")


class TestLegacyMigration(TempRootTest):
    """老格式(三个分开的文件)仍要能读进来并迁移."""

    def test_legacy_files_are_migrated_then_removed(self):
        up = folder("UP主下载", "123_测试UP")
        os.makedirs(up, exist_ok=True)
        with open(os.path.join(up, "已下载.json"), "w", encoding="utf-8") as f:
            json.dump({"9": {"title": "老的", "bvid": "BV9"}}, f)
        with open(os.path.join(up, "跳过.json"), "w", encoding="utf-8") as f:
            json.dump({"8": {"title": "老的跳过"}}, f)
        with open(os.path.join(up, "投稿列表.txt"), "w", encoding="utf-8") as f:
            f.write("#1 https://www.bilibili.com/video/av7 某个视频\n")

        st = State.load(up)
        self.assertEqual([v["aid"] for v in st.videos], ["7"])
        self.assertEqual(st.videos[0]["title"], "av7")
        self.assertEqual(list(st.record), ["9"])
        self.assertEqual(list(st.skip), ["8"])
        # 迁移完成后老文件应该被清掉, 只剩统一状态文件
        self.assertTrue(os.path.exists(paths.state_file(up)))
        for name in ("已下载.json", "跳过.json", "投稿列表.txt"):
            self.assertFalse(os.path.exists(os.path.join(up, name)))

    def test_no_legacy_files_means_no_migration(self):
        up = folder("UP主下载", "123_测试UP")
        os.makedirs(up, exist_ok=True)
        st = State.load(up)
        self.assertEqual(st.videos, [])
        self.assertFalse(os.path.exists(paths.state_file(up)))

    def test_temp_file_is_cleaned_on_load(self):
        up = folder("UP主下载", "123_测试UP")
        os.makedirs(up, exist_ok=True)
        tmp = paths.state_file(up) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("{}")
        State.load(up)
        self.assertFalse(os.path.exists(tmp))


class TestLocalFileCheck(TempRootTest):
    """本地是否已有这个视频 —— 判重逻辑(踩过最多的坑)."""

    def test_short_title_exact_match_is_found(self):
        """单字标题("选""腰")的文件存在时必须算已下载(这里曾经有过 bug)."""
        up = self.up_folder()
        touch(up, "选_100000000000005.mp4")
        self.assertTrue(media.video_file_exists(
            up, "100000000000005", "选", set()))

    def test_short_title_no_prefix_false_positive(self):
        """单字标题不能靠前缀乱认亲: 只有别的视频文件时应该算没下载."""
        up = self.up_folder()
        touch(up, "选美大赛_999.mp4")
        self.assertFalse(media.video_file_exists(
            up, "100000000000005", "选", set()))

    def test_exact_match_wins_for_normal_title(self):
        up = self.up_folder()
        touch(up, "正常视频_1.mp4")
        self.assertTrue(media.video_file_exists(up, "1", "正常视频", set()))

    def test_duplicate_title_does_not_use_prefix(self):
        """同标题出现两次时, 前缀匹配不可信, 只能靠精确匹配."""
        up = self.up_folder()
        touch(up, "同名的_111.mp4")
        self.assertFalse(media.video_file_exists(
            up, "222", "同名的", {"同名的"}))
        self.assertTrue(media.video_file_exists(
            up, "111", "同名的", {"同名的"}))

    def test_multi_part_subfolder_counts(self):
        """多P视频在 标题_aid\\ 子目录里, 也要算已下载."""
        up = self.up_folder()
        touch(up, os.path.join("多P视频_5", "[P01]第一集.mp4"))
        self.assertTrue(media.video_file_exists(up, "5", "多P视频", set()))
        # 空子目录不算
        os.makedirs(os.path.join(up, "空目录_6"), exist_ok=True)
        self.assertFalse(media.video_file_exists(up, "6", "空目录", set()))

    def test_cover_image_is_not_a_video(self):
        """只有封面图不算下载成功(踩过: 存了封面被当成下完了)."""
        up = self.up_folder()
        touch(up, "只有封面_7.jpg")
        self.assertFalse(media.video_file_exists(up, "7", "只有封面", set()))

    def test_is_successful_download_ignores_images(self):
        before = set()
        self.assertFalse(media.is_successful_download(before, {"a.jpg"}))
        self.assertTrue(media.is_successful_download(before, {"a.mp4"}))
        self.assertFalse(media.is_successful_download(before, {"a.m4s"}))

    def test_media_index_reuses_one_scan(self):
        up = self.up_folder()
        touch(up, "标题_1.mp4")
        index = media.MediaIndex(up)
        self.assertTrue(index.has("1", "标题"))
        touch(up, "另一个_2.mp4")
        self.assertFalse(index.has("2", "另一个"))   # 快照还没刷新
        index.refresh()
        self.assertTrue(index.has("2", "另一个"))

    def test_prefix_match_is_intentional_for_legacy_names(self):
        """老格式(只有标题、没有 _aid 后缀)只能靠前缀认, 这是有意为之."""
        up = self.up_folder()
        touch(up, "老格式标题.mp4")
        self.assertTrue(media.video_file_exists(up, "999", "老格式标题", set()))

    def test_reconcile_records_local_files(self):
        up = self.up_folder()
        touch(up, "补记这个_11.mp4")
        videos = [{"aid": "11", "bvid": "BV11", "title": "补记这个"},
                  {"aid": "12", "bvid": "BV12", "title": "真的没有"}]
        record = {}
        recovered = media.reconcile_media(up, videos, record)
        self.assertEqual(recovered, ["11"])
        self.assertEqual(record["11"]["time"], state_mod.TIME_RECOVERED)
        self.assertNotIn("12", record)

    def test_folder_bytes_counts_subdirs(self):
        root = tempfile.mkdtemp(prefix="bytes_test_")
        self.addCleanup(__import__("shutil").rmtree, root, ignore_errors=True)
        touch(root, "a.mp4")
        touch(root, os.path.join("标题_123", "p1.mp4"))
        touch(root, os.path.join("标题_123", "p2.mp4"))
        self.assertEqual(media.folder_bytes(root), 3)


class TestStateHelpers(TempRootTest):
    """几个独立的小读写."""

    def test_ensure_single_list_backfills_old_records(self):
        """老的单视频只有「已下载」没有名单, 要自动补进名单."""
        single = folder("单视频下载")
        write_state(single, [], record={"111": {"title": "老单视频",
                                                "bvid": "BV111"}})
        videos, record, _skip, _fail, _meta = state_mod.ensure_single_list(single)
        self.assertEqual([v["aid"] for v in videos], ["111"])
        self.assertEqual(videos[0]["title"], "老单视频")
        self.assertEqual(len(read_state(single)["投稿列表"]), 1)

    def test_pending_count_matches_state(self):
        up = folder("UP主下载", "123_测试UP")
        write_state(up, videos=[
            {"aid": "1", "bvid": "", "title": "a"},
            {"aid": "2", "bvid": "", "title": "b"},
            {"aid": "3", "bvid": "", "title": "c"},
        ], record={"1": {"title": "a"}}, skip={"2": {"title": "b"}})
        self.assertEqual(state_mod.pending_count(up), 1)

    def test_record_count_reads_state(self):
        up = folder("UP主下载", "123_测试UP")
        write_state(up, videos=[], record={"1": {}, "2": {}})
        self.assertEqual(state_mod.record_count(up), 2)


class TestFolderScanning(TempRootTest):
    """三类名单的扫描规则."""

    def test_three_folder_layout_is_covered(self):
        write_state(folder("UP主下载", "123_测试UP"),
                    [{"aid": "1", "bvid": "", "title": "视频1"}])
        write_state(folder("合集下载", "某某合集_1000001"),
                    [{"aid": "2", "bvid": "", "title": "直播回放"}])
        write_state(folder("单视频下载"), [], record={"9": {"title": "随手下的"}})
        names = tasks.scan_all_folders()
        self.assertEqual(names, [rel("UP主下载", "123_测试UP"),
                                 rel("合集下载", "某某合集_1000001")])

    def test_old_layout_up_folders_still_seen(self):
        """搬家之前直接躺在 videos\\ 下的老文件夹也得照旧认得."""
        write_state(folder("789_老布局"), [])
        write_state(folder("UP主下载", "123_新布局"), [])
        self.assertEqual(tasks.scan_up_folders(),
                         ["789_老布局", rel("UP主下载", "123_新布局")])

    def test_collection_without_state_is_not_a_list(self):
        os.makedirs(folder("合集下载", "空目录"), exist_ok=True)
        self.assertEqual(tasks.scan_collection_folders(), [])

    def test_empty_single_dir_is_not_counted(self):
        os.makedirs(folder("单视频下载"), exist_ok=True)
        self.assertEqual(tasks.scan_single(), 0)
        self.assertNotIn("单视频下载", tasks.scan_all_folders())

    def test_group_of_classifies(self):
        self.assertEqual(tasks.group_of(rel("UP主下载", "1_x")), "UP主下载")
        self.assertEqual(tasks.group_of(rel("合集下载", "x")), "合集下载")
        self.assertEqual(tasks.group_of("单视频下载"), "单视频下载")
        self.assertEqual(tasks.group_of("123_老布局"), "老布局UP主")

    def test_folder_to_mid_handles_subfolder_paths(self):
        self.assertEqual(tasks.folder_to_mid(rel("UP主下载", "123_测试UP")), "123")
        self.assertEqual(tasks.folder_to_mid("456_老布局"), "456")
        self.assertEqual(
            tasks.folder_to_mid(os.path.join("UP主下载", "789_名字")), "789")
        self.assertIsNone(tasks.folder_to_mid(rel("合集下载", "某某合集_1000001")))
        self.assertIsNone(tasks.folder_to_mid("单视频下载"))

    def test_new_up_folder_goes_to_up_dir(self):
        """新加的UP主必须落到 UP主下载\\ 下, 而不是 videos\\ 根目录."""
        path = tasks.up_folder_for("123", "某某")
        self.assertEqual(os.path.dirname(path), paths.up_dir())
        self.assertEqual(os.path.basename(path), "123_某某")

    def test_up_folder_name_is_sanitised(self):
        """名字里的非法字符要换掉, 否则建不出文件夹."""
        path = tasks.up_folder_for("123", "某某/名字")
        self.assertEqual(os.path.dirname(path), paths.up_dir())
        self.assertEqual(os.path.basename(path), "123_某某_名字")


if __name__ == "__main__":
    unittest.main()
