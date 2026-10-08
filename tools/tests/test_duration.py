# -*- coding: utf-8 -*-
"""时长排序(order=duration_small/duration_large)的测试.

要钉住的是四件事:

  · 两个接口的时长字段都要认对 —— 投稿列表给的是字符串 "00:07", 合集给的是
    秒数 199, 认不出来必须返回 None(宁可没有数据, 也不瞎猜一个时长);
  · 列表解析要顺手把时长存下来(不额外发请求), 认不出来就不存这个键;
  · 排序真的按**时长**而不是按**条数** —— 缺 5 条短视频的名单必须排在
    缺 1 条长视频的名单前面, 这是这个功能存在的唯一理由;
  · 没有时长数据时不能乱排: 名单自己能测平均就用它, 整份没有就用全库中位数,
    全库都没有(刚装上)就退化成"条数少的先跑"。
"""

import os
import unittest
from types import SimpleNamespace

from support import (RealProjectTest, TempRootTest, folder, write_state)
from test_entrypoints import load_entry

from bbdown_kit import (bilitools, duration, orders, state as state_mod, tasks,
                        util)

HOUR = 3600


def make_up(name, pending=0, downloaded=0, seconds=None, known=None):
    """造一份名单(UP主下载\\<name>), 返回文件夹路径.

    pending    待下载的条数
    downloaded 已下载的条数(只写记录, 不建文件 —— 时长排序不碰磁盘)
    seconds    每条视频的时长(秒); None = 老状态文件那样, 一个时长都没有
    known      前 known 条**待下载**有时长, 其余没有
               (默认全都一样: seconds=None 时一个都没有, 否则都有)
    """
    up = folder("UP主下载", name)
    videos, record = [], {}
    uid = name.split("_")[0]
    for i in range(downloaded):
        aid = "%s%02d" % (uid, i)
        entry = {"aid": aid, "title": "已下%s" % i}
        if seconds is not None:
            entry["duration"] = seconds
        videos.append(entry)
        record[aid] = {"title": "已下%s" % i, "time": ""}
    for i in range(pending):
        aid = "9%s%02d" % (uid, i)
        entry = {"aid": aid, "title": "没下%s" % i}
        if seconds is not None and (known is None or i < known):
            entry["duration"] = seconds
        videos.append(entry)
    write_state(up, videos, record=record)
    return up


def up_task(name, **kw):
    make_up(name, **kw)
    return ("existing", os.sep.join([tasks.UP, name]))


def names(tasks_):
    return [os.path.basename(t[1]) for t in tasks_]


class DurationFieldTest(unittest.TestCase):
    """bilitools.duration_of: 两个接口的时长字段."""

    def test_post_list_length_string(self):
        """投稿列表给的是 "MM:SS" / "H:MM:SS" 字符串."""
        self.assertEqual(bilitools.duration_of({"length": "00:07"}), 7)
        self.assertEqual(bilitools.duration_of({"length": "12:34"}), 754)
        self.assertEqual(bilitools.duration_of({"length": "1:02:03"}), 3723)
        self.assertEqual(bilitools.duration_of({"length": "10:00:00"}), 36000)

    def test_collection_duration_seconds(self):
        """合集给的是秒数(实测 199 / 176)."""
        self.assertEqual(bilitools.duration_of({"duration": 199}), 199)
        self.assertEqual(bilitools.duration_of({"duration": "176"}), 176)

    def test_seconds_win_over_the_string(self):
        self.assertEqual(
            bilitools.duration_of({"duration": 199, "length": "00:05"}), 199)

    def test_unknown_is_none_not_a_guess(self):
        """认不出来必须返回 None —— 宁可"这份名单没数据", 也不瞎猜一个时长."""
        for item in ({}, {"length": ""}, {"length": None}, {"length": "abc"},
                     {"length": "1:2:3:4"}, {"length": "0:00"},
                     {"duration": 0}, {"duration": None}, {"length": "-1:00"},
                     {"length": ":"}, {"duration": "x"}):
            self.assertIsNone(bilitools.duration_of(item), item)


class ParseStoresDurationTest(unittest.TestCase):
    """列表解析顺手存时长: 一次请求就把整页时长带回来了."""

    def test_post_list_page(self):
        page = {"data": {"list": {"vlist": [
            {"aid": 111, "bvid": "BV1", "title": "短的", "length": "00:07"},
            {"aid": 222, "bvid": "BV2", "title": "没时长"},
        ]}, "page": {"count": 2}}}
        items, count = bilitools.parse_page_items(page)
        self.assertEqual(count, 2)
        self.assertEqual(items[0]["duration"], 7)
        self.assertNotIn("duration", items[1],
                         "认不出来就别写这个键(别写 0 冒充数据)")

    def test_collection_page(self):
        page = {"data": {"archives": [
            {"aid": 333, "bvid": "BV3", "title": "合集里的", "duration": 199},
        ]}}
        items = bilitools.parse_archive_items(page["data"]["archives"])
        self.assertEqual(items[0]["duration"], 199)
        self.assertEqual(items[0]["aid"], "333")


class PendingSummaryTest(TempRootTest):
    """state.pending_summary: 待下载的条数/已知时长, 和整份名单的已知时长."""

    def test_counts_only_pending(self):
        up = make_up("111_测试UP", pending=3, downloaded=2, seconds=100)
        st = state_mod.State.load(up, readonly=True)
        st.skip["900001"] = {"title": "跳过的", "time": "x"}
        st.videos.append({"aid": "900001", "title": "跳过的", "duration": 5000})
        st.save()
        got = state_mod.pending_summary(up)
        self.assertEqual((got["count"], got["seconds"], got["known"]),
                         (3, 300, 3), "已下载/跳过的不算, 它们的时长也不该算")
        self.assertEqual((got["list_seconds"], got["list_known"]), (5500, 6),
                         "整份名单的已知时长含已下载和跳过的(算平均要用)")

    def test_old_state_without_duration(self):
        """老状态文件里没有 duration: 条数照样对, 时长是 0(排序会退回按条数)."""
        up = make_up("111_老名单", pending=4, seconds=None)
        got = state_mod.pending_summary(up)
        self.assertEqual(got["count"], 4)
        self.assertEqual((got["seconds"], got["known"]), (0, 0))
        self.assertEqual((got["list_seconds"], got["list_known"]), (0, 0))

    def test_pending_count_still_agrees(self):
        up = make_up("111_测试UP", pending=5, downloaded=1, seconds=60)
        self.assertEqual(state_mod.pending_count(up), 5)
        self.assertEqual(tasks.pending_of(("existing",
                                           os.sep.join([tasks.UP, "111_测试UP"]))), 5)

    def test_broken_entries_do_not_crash(self):
        """名单条目被手工改坏(字符串/没有 aid)也不能崩 —— 守护每轮都要调它."""
        up = make_up("111_脏数据", pending=1, seconds=60)
        st = state_mod.State.load(up)
        st.videos.extend(["坏条目", {"title": "没有 aid"}, {"aid": "777"}])
        st.save()
        got = state_mod.pending_summary(up)
        self.assertEqual(got["count"], 2)
        self.assertEqual(got["seconds"], 60)


class DurationEstimateTest(TempRootTest):
    """duration.pending_seconds: 已知的相加, 未知的按平均补."""

    def setUp(self):
        super().setUp()
        duration.reset()
        self.addCleanup(duration.reset)

    def test_all_known_is_exact(self):
        up = make_up("111_都知道", pending=4, seconds=90)
        duration.measure([up])
        self.assertEqual(duration.pending_seconds(up), 360)
        self.assertTrue(duration.has_data(up))

    def test_partial_uses_the_folder_average(self):
        up = make_up("111_一半知道", pending=4, seconds=100, known=2)
        duration.measure([up])
        self.assertEqual(duration.pending_seconds(up), 400,
                         "2 条已知 100 秒 + 2 条按 100 秒补")

    def test_pending_without_data_borrows_the_list_average(self):
        """待下载的一条时长都没有, 但整份名单有(已下载的那些): 用名单的平均补."""
        up = make_up("222_老待下载", pending=2, downloaded=3, seconds=HOUR)
        st = state_mod.State.load(up)
        for video in st.videos:
            if not video.get("aid", "").startswith("9"):
                continue
            video.pop("duration", None)      # 只有老待下载的那些没有时长
        st.save()
        duration.measure([up])
        got = state_mod.pending_summary(up)
        self.assertEqual((got["seconds"], got["known"]), (0, 0))
        self.assertEqual(duration.average_of(up), float(HOUR))
        self.assertEqual(duration.pending_seconds(up), 2 * HOUR)

    def test_folder_without_data_uses_library_median(self):
        known = make_up("111_有数据", pending=0, downloaded=2, seconds=600)
        blank = make_up("222_没数据", pending=3, seconds=None)
        duration.measure([known, blank])
        self.assertFalse(duration.has_data(blank))
        self.assertEqual(duration.typical_seconds(), 600)
        self.assertEqual(duration.pending_seconds(blank), 3 * 600)

    def test_nothing_known_anywhere_falls_back_to_count(self):
        """全库都没有时长(刚装上/还没完整校验): 退化成按条数, 不能乱排."""
        few = make_up("111_少", pending=1)
        many = make_up("222_多", pending=9)
        duration.measure([few, many])
        self.assertEqual(duration.typical_seconds(), 0)
        self.assertEqual(duration.pending_seconds(few), 1)
        self.assertEqual(duration.pending_seconds(many), 9)

    def test_no_pending_is_zero(self):
        up = make_up("111_没缺口", pending=0, downloaded=1, seconds=60)
        duration.measure([up])
        self.assertEqual(duration.pending_seconds(up), 0)
        self.assertEqual(duration.describe(0), "0 秒")

    def test_measure_reads_each_folder_only_once(self):
        up = make_up("111_甲", pending=1, seconds=60)
        self.assertEqual(duration.measure([up]), 1)
        self.assertEqual(duration.measure([up]), 0, "同一轮里不该重复统计")

    def test_stats_tells_how_many_folders_have_no_data(self):
        """日志要能说清"还有几个名单没时长数据" —— 不然用户会以为排错了."""
        known = make_up("111_有数据", pending=2, seconds=600)
        blank = make_up("222_没数据", pending=3)
        empty = make_up("333_没缺口", pending=0)
        duration.measure([known, blank, empty])
        total, work, without = duration.stats([known, blank, empty])
        self.assertEqual(work, 2, "没有缺口的不算'还有得下'")
        self.assertEqual(without, 1)
        self.assertEqual(total, 1200 + 3 * 600)


class DurationOrderTest(TempRootTest):
    """tasks.order_tasks: 按时长排和按条数排必须真的不一样."""

    def setUp(self):
        super().setUp()
        duration.reset()
        self.addCleanup(duration.reset)

    def test_duration_order_is_not_count_order(self):
        """缺 5 条短视频(共 5 分钟)要排在缺 1 条长视频(1 小时)的前面.

        按条数排正好相反(那边只差 1 条), 所以这条能证明"真的按时长在排"。
        """
        many_short = up_task("111_短视频", pending=5, seconds=60)
        one_long = up_task("222_长视频", pending=1, seconds=HOUR)

        short_first = tasks.order_tasks([many_short, one_long], [], [],
                                        "duration_small")
        self.assertEqual(names(short_first), ["111_短视频", "222_长视频"],
                         "按时长应该先跑短视频那个")

        # 老行为(smallest = 数条数)一个字都不能变
        by_count = tasks.order_tasks([many_short, one_long], [], [], "smallest")
        self.assertEqual(names(by_count), ["222_长视频", "111_短视频"],
                         "smallest 数的还是条数: 只差 1 条的先跑")

        long_first = tasks.order_tasks([many_short, one_long], [], [],
                                       "duration_large")
        self.assertEqual(names(long_first), ["222_长视频", "111_短视频"])

    def test_order_matches_the_estimate(self):
        """排序就是按 pending_seconds_of 的大小 —— 三个名单一起验一遍."""
        a = up_task("111_甲", pending=2, seconds=60)          # 120 秒
        b = up_task("222_乙", pending=1, seconds=600)         # 600 秒
        c = up_task("333_丙", pending=3, seconds=None, downloaded=0)  # 3×中位数
        got = tasks.order_tasks([c, b, a], [], [], "duration_small")
        sizes = [tasks.pending_seconds_of(t) for t in got]
        self.assertEqual(sizes, sorted(sizes))
        self.assertEqual(names(got)[0], "111_甲")

    def test_old_lists_without_duration_still_work(self):
        """一个时长都没有的库: 不能崩, 顺序退化成"条数少的先跑"."""
        few = up_task("111_少", pending=1)
        many = up_task("222_多", pending=9)
        got = tasks.order_tasks([many, few], [], [], "duration_small")
        self.assertEqual(names(got), ["111_少", "222_多"])

    def test_single_folder_stays_last(self):
        """单视频那点零头还是最后跑, 按时长排也不例外."""
        up = up_task("111_甲", pending=2, seconds=60)
        single = ("single", {"folder": folder(tasks.SINGLE), "count": 3})
        coll = ("collection", {"folder": folder(tasks.COLL, "某合集_1000001"),
                               "spec": {"类型": "合集", "mid": "1", "id": "2",
                                        "名称": "某合集"}})
        got = tasks.order_tasks([up], [coll], [single], "duration_small")
        self.assertEqual(got[-1], single)

    def test_other_orders_do_not_collect_durations(self):
        """默认顺序一分钱额外开销都不花: 不做时长统计."""
        up = up_task("111_甲", pending=1, seconds=60)
        calls = []
        real = duration.measure
        duration.measure = lambda folders, log_fn=None: calls.append(folders)
        try:
            tasks.order_tasks([up], [], [], "collection_first")
            tasks.order_tasks([up], [], [], "smallest")
            tasks.order_tasks([up], [], [], "name")
        finally:
            duration.measure = real
        self.assertEqual(calls, [], "不按时长排就不该统计时长")

    def test_preview_can_show_estimated_durations(self):
        up = up_task("111_甲", pending=3, seconds=120)
        ordered = tasks.order_tasks([up], [], [], "duration_small")
        self.assertEqual(tasks.order_preview(ordered), "111_甲")
        text = tasks.order_preview(ordered, durations=True)
        self.assertIn("≈6分00秒", text, "3 条 × 120 秒 = 6 分钟")

    def test_preview_skips_folders_with_nothing_to_download(self):
        """"没有缺口"的名单确实排在最前面, 但日志里不该只看到一串 0 秒."""
        empty = up_task("111_没缺口", pending=0, downloaded=1)
        work = up_task("222_有缺口", pending=1, seconds=60)
        ordered = tasks.order_tasks([work, empty], [], [], "duration_small")
        self.assertEqual(ordered[0], empty, "0 秒的确实排最前面")
        text = tasks.order_preview(ordered, durations=True)
        self.assertIn("222_有缺口", text)
        self.assertNotIn("111_没缺口", text)
        self.assertIn("另有 1 个名单没有缺口", text)


class OrderValueTest(unittest.TestCase):
    """orders 里的取值与别名."""

    def test_aliases_are_accepted(self):
        for text in ("duration_small", "DURATION_SMALL", " duration ",
                     "时长短的优先", "时长小的优先", "按时长", "时长从短到长"):
            self.assertEqual(orders.normalize_order(text), "duration_small", text)
        for text in ("duration_large", "时长长的优先", "时长从长到短"):
            self.assertEqual(orders.normalize_order(text), "duration_large", text)

    def test_old_bytes_values_still_work(self):
        """上一版设置文件里写的是 bytes_small/体积小的优先: 不能让它掉回默认顺序."""
        for text in ("bytes_small", "体积小的优先", "按体积"):
            self.assertEqual(orders.normalize_order(text), "duration_small", text)
        self.assertEqual(orders.normalize_order("bytes_large"), "duration_large")

    def test_unknown_value_still_falls_back_to_default(self):
        self.assertEqual(orders.normalize_order("胡说八道"),
                         orders.DEFAULT_ORDER)

    def test_every_order_value_has_a_label(self):
        """加了取值就必须同时有中文名 —— 否则日志里会打出一串英文."""
        for value in ("name", "collection_first", "smallest", "largest",
                      "duration_small", "duration_large"):
            self.assertIn(value, orders.ORDER_TEXT)
            self.assertEqual(orders.normalize_order(value), value,
                             "中文名有了, 别名表里也得有")

    def test_is_duration_order(self):
        self.assertTrue(orders.is_duration_order("duration_small"))
        self.assertTrue(orders.is_duration_order("duration_large"))
        for value in ("collection_first", "smallest", "largest", "name"):
            self.assertFalse(orders.is_duration_order(value), value)

    def test_chinese_alias_goes_through_the_settings_file(self):
        """设置文件里写中文也要认(手改设置的人不会记英文) —— 走真实的解析."""
        from bbdown_kit import config
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                         encoding="utf-8") as f:
            f.write("顺序=时长短的优先\n")
            path = f.name
        try:
            st = config.load_settings(path)
        finally:
            os.remove(path)
        self.assertEqual(st["order"], "duration_small")


class DurationTextTest(unittest.TestCase):
    """util.duration_text: 给人看的时长文字."""

    def test_ranges(self):
        self.assertEqual(util.duration_text(45), "45秒")
        self.assertEqual(util.duration_text(1830), "30分30秒")
        self.assertEqual(util.duration_text(7380), "2小时03分")
        self.assertEqual(util.duration_text(200000), "2天7小时")
        self.assertEqual(util.duration_text(-5), "0秒")


class SettingsAndDocsTest(RealProjectTest):
    """设置示例必须把新取值写出来 —— 不然用户根本不知道有这个东西."""

    def test_example_settings_document_every_order(self):
        text = self.read_text("下载设置.示例.txt")
        for value in orders.ORDER_TEXT:
            self.assertIn(value, text,
                          "下载设置.示例.txt 没写 order=%s" % value)


class ManagerPlanTest(TempRootTest):
    """管理器"一键更新"的计划阶段(守护每轮也走这条): 排序和日志都要对."""

    def setUp(self):
        super().setUp()
        duration.reset()
        self.addCleanup(duration.reset)

    def plan(self, order):
        """真的调用管理器的 plan_all, 顺便把日志行收下来."""
        manager = load_entry("BBDown-manager.py")
        lines = []
        real_log = manager.log
        manager.log = lambda msg: lines.append(str(msg))
        try:
            plan = manager.plan_all(SimpleNamespace(order=order, yes=False),
                                    "测试")
        finally:
            manager.log = real_log
        return plan, "\n".join(lines)

    def test_duration_order_logs_what_it_is_doing(self):
        make_up("111_短", pending=5, seconds=60)
        make_up("222_长", pending=1, seconds=HOUR)
        plan, text = self.plan("duration_small")
        self.assertEqual(names(plan.tasks), ["111_短", "222_长"], "短的排前面")
        self.assertIn("时长短的优先", text, "日志要说清用的是哪种顺序")
        self.assertIn("时长统计:", text, "要说清一共要下多久")
        self.assertIn("111_短 ≈5分00秒", text, "5 条 × 60 秒")
        self.assertIn("222_长 ≈1小时00分", text)

    def test_log_is_honest_when_no_duration_exists_at_all(self):
        """一条时长都没有时不许报一个假的"合计约 X 小时" —— 那只是条数当秒数."""
        make_up("111_老名单", pending=3, seconds=None)
        _plan, text = self.plan("duration_small")
        self.assertIn("一个时长数据都还没有", text)
        self.assertIn("先按缺口条数排", text)
        self.assertIn("--full --limit 0", text, "要告诉用户怎么现在补齐")
        self.assertNotIn("合计约", text, "没有数据就别装出一个总数")

    def test_log_counts_folders_that_still_have_no_duration(self):
        """部分名单有数据时: 报合计, 并说清还有几个名单没数据."""
        make_up("111_有数据", pending=2, seconds=HOUR)
        make_up("222_没数据", pending=3, seconds=None)
        _plan, text = self.plan("duration_small")
        self.assertIn("合计约", text)
        self.assertIn("其中 1 个名单还没有时长数据", text)

    def test_other_orders_log_nothing_extra(self):
        make_up("111_甲", pending=1, seconds=60)
        _plan, text = self.plan("collection_first")
        self.assertNotIn("时长统计", text)

    def test_menu_full_check_also_follows_the_order(self):
        """菜单『F 全部完整校验』以前直接用文件夹名顺序 —— 现在也得按 order 排.

        (守护和一键更新走的是 plan_all, 那条路本来就排; 这条是补上漏掉的那条。)
        """
        manager = load_entry("BBDown-manager.py")
        make_up("111_长", pending=1, seconds=HOUR)
        make_up("222_短", pending=1, seconds=60)
        real_pick = manager.select_up_folders
        manager.select_up_folders = lambda folders: "FULL"
        try:
            plan = manager.collect_tasks(
                SimpleNamespace(order="duration_small", all=False, video=None,
                                collection=None, url=None, yes=False, full=False),
                None)
        finally:
            manager.select_up_folders = real_pick
        self.assertEqual(names(plan.tasks), ["222_短", "111_长"])
        self.assertEqual([t[0] for t in plan.tasks], ["existing", "existing"])


if __name__ == "__main__":
    unittest.main()
