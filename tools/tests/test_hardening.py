# -*- coding: utf-8 -*-
"""硬化补丁的回归测试: 每一条都钉住一个"修好了但很容易改回去"的行为.

都是离线用例(不联网、不碰真实目录), 跟其它测试一样在临时目录里跑。

注意: 这里要测的字符包括 DEL/C1/行分隔符/孤立代理字符, 它们肉眼看不见,
所以一律用 chr() 拼出来, 不要在源码里直接写这些字符 —— 源码保持可见可改。
"""

import importlib.util
import os
import subprocess
import sys
import time
import unittest

from support import (REAL_TOOLS, TempRootTest, folder, read_state, touch,
                     write_state)

from bbdown_kit import (bilitools, download, logging as kit_log, maintenance,
                        media, paths, procs, runner, state as state_mod, util)


def _load_guard():
    """按路径加载 下载守护.py(文件名是中文, 不能直接 import)."""
    path = os.path.join(REAL_TOOLS, "下载守护.py")
    spec = importlib.util.spec_from_file_location("guard_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_manager():
    """按路径加载 BBDown-manager.py."""
    path = os.path.join(REAL_TOOLS, "BBDown-manager.py")
    spec = importlib.util.spec_from_file_location("manager_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HeaderSpySession(object):
    """带 headers 的假会话: 记下每次请求的目标和本次请求头."""

    def __init__(self):
        self.headers = {"Cookie": "SESSDATA=SECRET", "User-Agent": "x"}
        self.calls = []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        resp = type("R", (object,), {})()
        resp.url = "https://www.bilibili.com/video/BV1DGDpBgEos"
        return resp


class TestShortLinkHostWhitelist(unittest.TestCase):
    """短链只认真正的 b23.tv 域名, 而且请求它时不带登录 Cookie."""

    def test_only_real_b23_host_is_fetched(self):
        session = HeaderSpySession()
        bilitools.resolve_short_link(session, "https://b23.tv.evil.com/BV1x")
        bilitools.resolve_short_link(session, "https://evil.com/?x=b23.tv")
        self.assertEqual(session.calls, [], "非 b23.tv 域名不该被请求")

    def test_real_b23_host_is_fetched_without_cookie(self):
        session = HeaderSpySession()
        got = bilitools.resolve_short_link(session, "https://b23.tv/abcd")
        self.assertEqual(got, "https://www.bilibili.com/video/BV1DGDpBgEos")
        self.assertEqual(len(session.calls), 1)
        _url, kw = session.calls[0]
        self.assertEqual(kw.get("headers"), {"Cookie": None},
                         "请求短链必须显式摘掉 Cookie 头")
        self.assertEqual(session.headers["Cookie"], "SESSDATA=SECRET",
                         "会话上的 Cookie 不能被破坏(后面还要调接口)")

    def test_scheme_less_paste_is_fetched_with_scheme(self):
        """无协议头的 b23.tv/xxx 要补成 https:// 再请求.

        以前是"用补全后的地址判断是不是短链, 却拿原字符串去请求": 校验通过、
        requests 抛 MissingSchema 被吞掉, 于是粘贴无协议短链静默不工作。
        假会话不会自己去校验 URL, 所以这里直接看请求出去的是什么地址。
        """
        session = HeaderSpySession()
        bilitools.resolve_short_link(session, "b23.tv/abcd")
        self.assertEqual(len(session.calls), 1)
        url, _kw = session.calls[0]
        self.assertTrue(url.startswith("https://"), "请求地址缺协议头: %r" % url)
        self.assertEqual(url, "https://b23.tv/abcd")

    def test_host_of_ignores_port_and_userinfo(self):
        self.assertEqual(bilitools.host_of("https://b23.tv:8080/x"), "b23.tv")
        self.assertEqual(bilitools.host_of("https://b23.tv@evil.com/x"),
                         "evil.com")
        self.assertEqual(bilitools.host_of("b23.tv/x"), "b23.tv")
        self.assertEqual(bilitools.host_of(""), "")

    def test_classify_link_keeps_old_answers(self):
        self.assertEqual(bilitools.classify_link(
            "https://space.bilibili.com/123"), "up")
        self.assertEqual(bilitools.classify_link("https://b23.tv/abcd"),
                         "video")
        self.assertIsNone(bilitools.classify_link("https://example.com"))
        self.assertEqual(bilitools.classify_link(
            "https://www.bilibili.com/video/BV1DGDpBgEos"), "video")

    def test_collection_type_is_parsed_not_substring_matched(self):
        spec = bilitools.parse_collection_link(
            "https://space.bilibili.com/1/lists/2?type=season&from=type=series")
        self.assertEqual(spec["类型"], "合集")
        self.assertEqual(spec["id"], "2")
        old = bilitools.parse_collection_link(
            "https://space.bilibili.com/1/channel/collectiondetail?sid=8888")
        self.assertEqual(old, {"类型": "合集", "mid": "1", "id": "8888"})
        series = bilitools.parse_collection_link(
            "space.bilibili.com/1/lists/2?type=series")
        self.assertEqual(series["类型"], "系列")


class TestCleanText(TempRootTest):
    """远端文本里的控制字符要替换掉, 但不能改动别的任何字符."""

    def test_control_chars_become_underscore(self):
        self.assertEqual(util.clean_text("a\x00b\x1b[31mc\td"),
                         "a_b_[31mc_d")
        self.assertEqual(util.clean_text("a\nb"), "a_b")
        self.assertEqual(util.clean_text(None), "")

    def test_normal_text_and_spaces_untouched(self):
        text = "【4K】少女时代 完颜团翻跳，爷青回！🚴"
        self.assertEqual(util.clean_text(text), text)

    def test_title_with_tab_matches_bbdown_filename(self):
        """BBDown 把 \\t 写成下划线(实测), 清洗后的标题必须还原出同一个名字."""
        title = "骑行vlog\t路上的风景"
        self.assertEqual(util.sanitize_name(util.clean_text(title)),
                         util.sanitize_name(title))


class TestInvisibleCharsAreCleaned(TempRootTest):
    """日志/文件名的出口: 看不见但能坏事的字符也要换掉."""

    def test_del_c1_and_line_separators(self):
        self.assertEqual(util.clean_text("a" + chr(0x7F) + "b"), "a_b")   # DEL
        self.assertEqual(util.clean_text("a" + chr(0x9B) + "b"), "a_b")   # C1
        sep = chr(0x2028) + "b" + chr(0x2029)      # 行分隔符
        self.assertEqual(util.clean_text("a" + sep + "c"), "a_b_c")

    def test_lone_surrogate_does_not_crash_everything(self):
        """JSON 里 \\ud800 是合法的, 但直接写文件/打印会抛异常."""
        got = util.clean_text("a" + chr(0xD800) + "b")
        self.assertEqual(got, "a_b")
        got.encode("utf-8")            # 清洗后必须是能编码的字符串

    def test_log_cannot_forge_a_line(self):
        path = os.path.join(self.tmp, "log.txt")
        kit_log.log("标题\n[00:00:00] 伪造的一行\r尾部", to_file=path)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(len(text.strip().splitlines()), 1, "日志里不许出现第二行")
        self.assertNotIn("\r", text)
        self.assertIn("标题", text)


class TestForgedEventIsNotTrusted(TempRootTest):
    """子进程 stdout 里的哨兵文本不能被当成事件(只有事件文件才是)."""

    FAKE = (
        "# uses the bbdown_kit protocol\n"
        "import time\n"
        "print('\\x00EVENT\\x00{\"kind\": \"video_end\", \"aid\": \"1\","
        " \"ok\": true}')\n"
        "time.sleep(0.3)\n"
    )

    def test_stdout_sentinel_stays_a_log_line(self):
        path = os.path.join(self.tmp, "fake-manager.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.FAKE)
        log_dir = os.path.join(self.tmp, "round")
        round_log = os.path.join(log_dir, "round.txt")
        _start, result = runner.run_round(
            manager_cmd=[sys.executable, path],
            round_no=1, quarantined={}, log_dir=log_dir,
            round_log_path=round_log,
            events_path=os.path.join(log_dir, "round.events.jsonl"),
            manager_path=path,
        )
        self.assertEqual(result.downloads, 0, "伪造的 video_end 不能被算成功")
        self.assertEqual(result.fails, 0)
        self.assertIsNone(result.killed_reason)
        with open(round_log, encoding="utf-8") as f:
            self.assertIn("EVENT", f.read(), "那一行应该照常进人类可读日志")


class TestProcessIdentityChecks(TempRootTest):
    """从文本文件里读到 pid 不能直接杀, 必须先核对命令行."""

    def test_stranger_pid_is_not_ours(self):
        got = procs.process_is_ours(os.getpid(), ("BBDown-manager.py",))
        self.assertIn(got, (False, None))

    def test_matching_cmdline_is_ours(self):
        if procs._psutil() is None:
            self.skipTest("没有 psutil")
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(5)",
             "BBDown-manager.py"])
        try:
            time.sleep(0.3)
            self.assertIs(procs.process_is_ours(proc.pid,
                                                ("BBDown-manager.py",)), True)
        finally:
            proc.kill()
            proc.wait()

    def test_foreign_guard_script_is_not_ours(self):
        """别处的同名脚本不能被当成我们的守护(标记要用绝对路径)."""
        self.assertFalse(procs._guard_marker_hit(
            "python c:" + os.sep + "other" + os.sep + "guard.py"))
        self.assertTrue(procs._guard_marker_hit(
            os.path.normcase(paths.guard_script()).lower()))

    def test_garbage_holder_is_rejected_but_lock_still_truthy(self):
        path = os.path.join(self.tmp, "x.lock")
        who = path + ".who"
        holder = procs.FileLock(path, who)
        self.assertTrue(holder.acquire())
        try:
            with open(who, "w", encoding="utf-8") as f:
                f.write("不是数字\n")
            self.assertIsNone(procs.FileLock(path, who).read_holder())
            with open(who, "w", encoding="utf-8") as f:
                f.write("99999999999999999999\n")
            self.assertIsNone(procs.FileLock(path, who).read_holder())
            with open(who, "w", encoding="utf-8") as f:
                f.write("12345 2026-01-01 00:00:00\n")
            self.assertEqual(procs.FileLock(path, who).read_holder(), 12345)
            # 拿不到锁时返回的必须是真值, 否则调用方会把"有锁"当成"没锁"
            second = procs.HeldLock(path, who)
            with open(who, "w", encoding="utf-8") as f:
                f.write("坏名片\n")
            got = second.acquire(wait_seconds=0)
            self.assertTrue(got, "拿不到锁时必须返回真值")
            self.assertEqual(got, procs.UNKNOWN_HOLDER)
        finally:
            holder.release()


class TestLockProbeAndKillGuards(TempRootTest):
    """查锁不许有写副作用; 结束进程前必须核对身份."""

    def test_probe_never_creates_files(self):
        lock = os.path.join(self.tmp, "x.lock")
        who = lock + ".who"
        self.assertIsNone(procs.lock_holder(lock, who))
        self.assertFalse(os.path.exists(lock), "只查询不该建出锁文件")
        self.assertFalse(os.path.exists(who), "只查询不该写出名片")

    def test_probe_reports_the_holder(self):
        lock = os.path.join(self.tmp, "y.lock")
        who = lock + ".who"
        holder = procs.FileLock(lock, who)
        self.assertTrue(holder.acquire())
        try:
            self.assertEqual(procs.lock_holder(lock, who), os.getpid())
        finally:
            holder.release()

    def test_probe_does_not_rewrite_an_unlocked_lock_file(self):
        """没人占的时候也不能动它.

        以前是"真去 acquire 一把": 拿到锁就把锁文件截断、写上自己的 pid,
        于是一次只读查询把上次留下的内容改掉了(而且锁文件里的 pid 正是
        排查"谁在跑"的线索)。
        """
        lock = os.path.join(self.tmp, "w.lock")
        who = lock + ".who"
        with open(lock, "wb") as f:
            f.write(b"12345")            # 上次崩溃留下的旧内容
        self.assertIsNone(procs.lock_holder(lock, who))
        with open(lock, "rb") as f:
            self.assertEqual(f.read(), b"12345", "查询不该改写锁文件内容")
        self.assertFalse(os.path.exists(who), "查询不该写出名片")

    def test_who_file_has_no_leftover_tmp(self):
        lock = os.path.join(self.tmp, "z.lock")
        who = lock + ".who"
        holder = procs.FileLock(lock, who)
        self.assertTrue(holder.acquire())
        try:
            self.assertEqual(procs.FileLock(lock, who).read_holder(), os.getpid())
            leftovers = [n for n in os.listdir(self.tmp) if n.endswith(".tmp")]
            self.assertEqual(leftovers, [], "写名片不该留下临时文件")
        finally:
            holder.release()

    def test_verify_callback_can_refuse_the_kill(self):
        if procs._psutil() is None:
            self.skipTest("没有 psutil")
        proc = subprocess.Popen([sys.executable, "-c",
                                 "import time; time.sleep(5)"])
        try:
            procs.kill_tree(proc.pid, "测试", verify=lambda _pid: False)
            time.sleep(0.3)
            self.assertIsNone(proc.poll(), "身份核对不通过时不许动手")
        finally:
            proc.kill()
            proc.wait()


class TestMaintenanceNeverLosesVideos(TempRootTest):
    """「跳过」是唯一会丢视频的地方: 只有 permanent 才配永远不重试."""

    def _folder_with(self, aid="1", title="只失败一次"):
        up = self.up_folder()
        write_state(up, videos=[{"aid": aid, "bvid": "", "title": title}])
        return up

    def test_failed_once_is_not_force_skipped(self):
        up = self._folder_with()
        quarantined = {"1": {"fails": 1, "title": "只失败一次"}}
        maintenance.prepare_round(quarantined, do_ghost=False)
        self.assertNotIn("1", read_state(up)["跳过"],
                         "只是失败计数, 不是永久失败, 不能被写进跳过")

    def test_permanent_still_skipped(self):
        up = self._folder_with()
        quarantined = {"1": {"permanent": True, "title": "充电专属"}}
        maintenance.prepare_round(quarantined, do_ghost=False)
        self.assertIn("1", read_state(up)["跳过"])
        maintenance.prepare_round(quarantined, do_ghost=False)
        self.assertIn("1", read_state(up)["跳过"], "永久失败必须一直留着")

    def test_non_permanent_skip_is_requeued(self):
        up = self._folder_with()
        st = state_mod.State.load(up)
        st.force_skip("1", "只失败一次", "2026-01-01 00:00:00")
        st.save()
        maintenance.prepare_round({"1": {"fails": 2}}, do_ghost=False)
        self.assertNotIn("1", read_state(up)["跳过"], "计数没到永久就该放回队列")

    def test_guard_marker_is_never_counted_as_this_round(self):
        """守护写的中文标记不是时间戳, 不能每轮都给失败次数 +1."""
        up = self._folder_with()
        st = state_mod.State.load(up)
        st.force_skip("1", "只失败一次", state_mod.TIME_PERMANENT)
        st.save()
        quarantined = {}
        maintenance.record_round_failures("2026-09-30 00:00:00", quarantined)
        self.assertEqual(quarantined, {})
        maintenance.undo_round_damage("2026-09-30 00:00:00", quarantined)
        self.assertIn("1", read_state(up)["跳过"],
                      "永久标记不该被当成'本轮新写的'撤回")

    def test_real_timestamps_still_work(self):
        up = self._folder_with()
        st = state_mod.State.load(up)
        st.force_skip("1", "限流误伤", "2026-09-29 10:00:00")
        st.save()
        quarantined = {}
        maintenance.record_round_failures("2026-09-29 09:30:00", quarantined)
        self.assertEqual(quarantined["1"]["fails"], 1)
        maintenance.undo_round_damage("2026-09-29 09:30:00", quarantined)
        self.assertNotIn("1", read_state(up)["跳过"], "本轮写的该被撤回")


class TestRoundDamageRollsBackCounters(TempRootTest):
    """限流轮当没发生过: 「跳过」和「失败」的计数必须一起撤."""

    def test_failure_count_from_this_round_is_rolled_back(self):
        up = self.up_folder()
        write_state(up, videos=[{"aid": "1", "bvid": "", "title": "被限流"}])
        st = state_mod.State.load(up)
        st.mark_failed("1", "被限流", when="2026-09-29 10:00:00")
        st.save()

        quarantined = {}
        maintenance.record_round_failures("2026-09-29 09:30:00", quarantined)
        maintenance.undo_round_damage("2026-09-29 09:30:00", quarantined)
        st = state_mod.State.load(up)
        self.assertEqual(st.failed, {}, "本轮留下的失败计数也要撤掉")
        self.assertEqual(st.skip, {})


class TestQuarantineLifecycle(TempRootTest):
    """永久失败名录: 已下载的要忘掉, 用户点名重试的要能手动清."""

    def test_prune_drops_already_downloaded(self):
        data = {"1": {"title": "下好了"}, "2": {"title": "还在名单里"}}
        gone = maintenance.prune_quarantine(data, {"1", "2"}, {"1"})
        self.assertEqual(gone, ["1"])
        self.assertNotIn("1", data)
        self.assertIn("2", data)

    def test_forget_permanent_clears_named_aids(self):
        path = paths.quarantine_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"1": {"permanent": true}, "2": {"permanent": true}}')
        gone = maintenance.forget_permanent(["1"])
        self.assertEqual(gone, ["1"])
        left = state_mod.load_json(path, {})
        self.assertNotIn("1", left)
        self.assertIn("2", left)


class TestBrokenStateFileIsKept(TempRootTest):
    """状态文件读不出来时必须留证, 绝不能被当成空状态盖掉."""

    def test_broken_json_is_renamed_not_overwritten(self):
        up = self.up_folder()
        os.makedirs(up, exist_ok=True)
        path = paths.state_file(up)
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"已下载": {"1": {"title": "第 1 个"}}, "坏在')  # 截断的 JSON

        st = state_mod.State.load(up)
        self.assertEqual(st.record, {}, "坏了就当空的, 但下面的文件必须留着")
        self.assertFalse(os.path.exists(path), "坏文件该被改名挪开")
        kept = [n for n in os.listdir(up) if ".bad-" in n]
        self.assertEqual(len(kept), 1, "坏掉的原文件必须留一份")
        with open(os.path.join(up, kept[0]), encoding="utf-8") as f:
            self.assertIn("第 1 个", f.read(), "原始内容不能在改名时丢掉")

    def test_readonly_load_never_renames(self):
        up = self.up_folder()
        os.makedirs(up, exist_ok=True)
        path = paths.state_file(up)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{坏")
        state_mod.State.load(up, readonly=True)
        self.assertTrue(os.path.exists(path), "--status/--audit 不许动文件")


class TestArchiveLine(TempRootTest):
    """每轮日志的行尾不该多出下划线(子进程输出的是 CRLF)."""

    def test_crlf_becomes_one_newline(self):
        self.assertEqual(runner._archive_line("hello\r\n"), "hello\n")
        self.assertEqual(runner._archive_line("no newline"), "no newline")

    def test_control_chars_still_cleaned(self):
        self.assertEqual(runner._archive_line("a\x00b\x1b[31m\r\n"),
                         "a_b_[31m\n")


class TestMediaAttribution(TempRootTest):
    """判重与"下载成功"的判定都必须按视频自己对号."""

    def test_short_title_does_not_swallow_longer_names(self):
        up = self.up_folder()
        touch(up, "预告片_123.mp4")
        self.assertFalse(media.video_file_exists(up, "999", "预告", set()))
        touch(up, "预告_124.mp4")
        self.assertTrue(media.video_file_exists(up, "124", "预告", set()))

    def test_legacy_exact_name_still_matches(self):
        up = self.up_folder()
        touch(up, "老格式标题.mp4")
        self.assertTrue(media.video_file_exists(up, "999", "老格式标题", set()))

    def test_multi_part_folder_boundary(self):
        up = self.up_folder()
        touch(up, "多P视频集锦_9/[P01]a.mp4")
        self.assertFalse(media.video_file_exists(up, "8", "多P视频", set()))
        self.assertTrue(media.video_file_exists(up, "9", "多P视频集锦", set()))
        touch(up, "多P视频_5/[P01]第一集.mp4")
        self.assertTrue(media.video_file_exists(up, "5", "多P视频", set()))

    def test_new_files_are_attributed_per_video(self):
        after = {"别人的_9.mp4", "我的_7.mp4", "我的_7/[P01]a.mp4"}
        got = media.new_video_files(set(), after, "7", "我的")
        self.assertEqual(got, {"我的_7.mp4", "我的_7/[P01]a.mp4"})
        self.assertEqual(media.new_video_files(set(), after, "8", "不存在的"),
                         set())

    def test_parallel_foreign_file_is_not_my_success(self):
        """并行下载时别人落盘的文件不能算我成功(download_one 端到端)."""
        up = self.up_folder()
        os.makedirs(up, exist_ok=True)

        def fake_bbdown(url, folder_, extra_args, timeout=None, aid=None):
            with open(os.path.join(folder_, "别人的_999.mp4"), "wb") as f:
                f.write(b"x")
            return 0

        original = download.run_bbdown
        download.run_bbdown = fake_bbdown
        try:
            video = {"aid": "7", "bvid": "", "title": "我的视频"}
            _v, ok, _rc, new_file, _took = download.download_one(
                up, video, [], set())
        finally:
            download.run_bbdown = original
        self.assertFalse(new_file)
        self.assertFalse(ok, "别人的新文件不能当成我的下载成功")


class TestSuccessNeedsARealVideo(TempRootTest):
    """「下载成功」只认这个视频自己的成片, 封面/音频/0字节残骸都不算."""

    def test_cover_only_is_not_a_new_download(self):
        after = {"我的_7.mp4", "我的_7.jpg", "我的_7.xml", "我的_7.danmaku"}
        self.assertEqual(media.new_video_files(set(), after, "7", "我的"),
                         {"我的_7.mp4"})

    def test_cover_only_does_not_mark_success_end_to_end(self):
        up = self.up_folder()
        os.makedirs(up, exist_ok=True)

        def fake_bbdown(url, folder_, extra_args, timeout=None, aid=None):
            with open(os.path.join(folder_, "我的视频_7.jpg"), "wb") as f:
                f.write(b"cover")
            return 0

        original = download.run_bbdown
        download.run_bbdown = fake_bbdown
        try:
            video = {"aid": "7", "bvid": "", "title": "我的视频"}
            _v, ok, _rc, new_file, _took = download.download_one(
                up, video, [], set())
        finally:
            download.run_bbdown = original
        self.assertFalse(ok, "只下到封面不能算成功, 否则会永远不再重试")

    def test_zero_byte_leftover_is_not_downloaded(self):
        up = self.up_folder()
        touch(up, "断在半路_7.mp4", data=b"")
        self.assertFalse(media.video_file_exists(up, "7", "断在半路", set()),
                         "0 字节成片是残骸, 不该当成已下载")

    def test_other_videos_exact_name_is_not_swallowed(self):
        """标题"预告"的文件库里, "预告_123.mp4" 是别人(aid=123)的成片."""
        up = self.up_folder()
        touch(up, "预告_123.mp4")
        self.assertFalse(media.video_file_exists(up, "999", "预告", set()))
        self.assertTrue(media.video_file_exists(up, "123", "预告", set()))


class TestDataRootGuard(TempRootTest):
    """data_root 设置写坏了要退回默认, 不能把状态文件撒进系统目录."""

    def test_dangerous_paths_are_rejected(self):
        default = paths._candidate_data_root("")
        for bad in ("C:\\", "C:/Windows/Temp", "C:/Program Files/x"):
            self.assertEqual(paths._candidate_data_root(bad), default)
        self.assertFalse(paths._data_root_ok("C:\\"))
        self.assertFalse(paths._data_root_ok("C:"))
        self.assertTrue(paths._data_root_ok("\\\\server\\share"),
                        "网络共享不是系统目录, 不该被拒绝")

    def test_device_paths_and_short_names_are_rejected(self):
        """\\\\?\\C:\\... 和 8.3 短名都能绕过只看字符串的判断."""
        bs = chr(92)
        device = bs + bs + "?" + bs + "C:" + bs + "Windows" + bs + "Temp"
        device_dot = bs + bs + "." + bs + "C:" + bs + "Windows"
        self.assertFalse(paths._data_root_ok(device))
        self.assertFalse(paths._data_root_ok(device_dot))
        if os.path.exists("C:\\PROGRA~1"):
            self.assertFalse(paths._data_root_ok("C:\\PROGRA~1"),
                             "8.3 短名要能解析回真正的系统目录")

    def test_home_dir_area_is_rejected_but_deeper_is_fine(self):
        """用户目录"附近"要拦, 但更深的正常项目位置要放行.

        这是收尾排查时发现的缺口: 原来只拦系统目录, 于是把 视频目录 设成
        "C:\\Users"(或家目录本身)是允许的 —— 那种情况下 单视频下载\\ 这类
        目录会直接撒进用户目录里。两个方向都要拦: 家目录的父目录和它的
        直属子目录; 再深就放行(用户完全可能把项目放在 文档\\xxx\\BBDown)。
        """
        home = os.path.expanduser("~")
        parent = os.path.dirname(home)
        self.assertFalse(paths._data_root_ok(home), "家目录本身不该被接受")
        if parent and parent != home:
            self.assertFalse(paths._data_root_ok(parent),
                             "家目录的父目录(C:\\Users)不该被接受")
        self.assertFalse(paths._data_root_ok(os.path.join(home, "Videos")),
                         "家目录的直属子目录不该被接受")
        self.assertTrue(
            paths._data_root_ok(os.path.join(home, "Videos", "BBDown")),
            "家目录下三层的正常项目位置必须放行")

    def test_normal_paths_are_kept(self):
        self.assertTrue(paths._data_root_ok("D:/bili_videos"))
        self.assertEqual(paths._candidate_data_root("videos"),
                         os.path.join(paths.ROOT, "videos"))


class TestReadonlyLoad(TempRootTest):
    """--status / --audit 用的只读加载: 一个字节都不写."""

    def test_readonly_does_not_delete_tmp(self):
        up = self.up_folder()
        write_state(up, videos=[{"aid": "1", "bvid": "", "title": "a"}])
        tmp = paths.state_file(up) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("写了一半")

        st = state_mod.State.load(up, readonly=True)
        self.assertEqual(len(st.videos), 1)
        self.assertTrue(os.path.exists(tmp), "只读加载不该删 .tmp")

        state_mod.State.load(up)                # 普通加载照旧清理
        self.assertFalse(os.path.exists(tmp))

    def test_readonly_does_not_migrate_legacy(self):
        up = self.up_folder()
        os.makedirs(up, exist_ok=True)
        legacy = os.path.join(up, paths.LEGACY_RECORD_NAME)
        with open(legacy, "w", encoding="utf-8") as f:
            f.write('{"1": {"title": "老的"}}')

        st = state_mod.State.load(up, readonly=True)
        self.assertEqual(st.record, {})
        self.assertTrue(os.path.exists(legacy), "只读加载不该做迁移")

        state_mod.State.load(up)                # 普通加载会迁移老格式
        self.assertFalse(os.path.exists(legacy))
        self.assertTrue(os.path.exists(paths.state_file(up)))


class TestManagerArgs(TempRootTest):
    """--bbdown-args 要按 Windows 习惯切, 不能吃掉反斜杠/抛栈."""

    def test_backslashes_survive_and_bad_quotes_do_not_crash(self):
        manager = _load_manager()
        self.assertEqual(manager.split_extra_args(r"--work-dir C:\temp\x"),
                         ["--work-dir", r"C:\temp\x"])
        self.assertEqual(manager.split_extra_args('--a "b c" --d'),
                         ["--a", "b c", "--d"])
        self.assertEqual(manager.split_extra_args("--x 1 --y 2"),
                         ["--x", "1", "--y", "2"])
        self.assertEqual(manager.split_extra_args("   "), [])
        self.assertEqual(manager.split_extra_args('"没配对的引号'),
                         ['"没配对的引号'])

    def test_help_is_not_swallowed(self):
        manager = _load_manager()
        _args, unknown = manager.build_parser().parse_known_args(["--help"])
        self.assertIn("--help", unknown,
                      "--help 必须能被 run_manager 看到(以前被静默丢掉)")


class TestGuardLogRotation(TempRootTest):
    """日志归档和清理必须在同一个目录(以前清理看的是 tools\\, 从没生效)."""

    def test_archives_are_pruned_in_log_dir(self):
        guard = _load_guard()
        for i in range(5):
            name = "守护日志.2026010%d-000000.txt" % i
            with open(os.path.join(paths.log_root(), name), "w",
                      encoding="utf-8") as f:
                f.write("x")
        with open(paths.guard_log(), "w", encoding="utf-8") as f:
            f.write("x" * (guard.GUARD_LOG_MAX_BYTES + 1))
        guard.rotate_guard_log("20260930-010101")
        names = sorted(n for n in os.listdir(paths.log_root())
                       if n.startswith("守护日志.") and n.endswith(".txt"))
        self.assertEqual(len(names), guard.GUARD_LOG_KEEP)
        self.assertIn("守护日志.20260930-010101.txt", names)


if __name__ == "__main__":
    unittest.main()
