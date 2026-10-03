# -*- coding: utf-8 -*-
"""安全停止(stop_safely.py)的测试.

重点钉住那条**误杀**的教训: 进程匹配必须带完整路径, 只认"当前这一份"项目。
曾经为了兼容沙盒加过不带路径的宽松匹配("bbdown-manager.py"), 结果测试脚本
自己起的沙盒进程, 把**真实项目正在跑的管理器**一起收掉了。
"""

import importlib.util
import os
import sys
import time
import unittest

from support import (REAL_ROOT, REAL_TOOLS, TempRootTest, folder, paths,
                     write_state)

STOP_PY = os.path.join(REAL_TOOLS, "stop_safely.py")

# 注意基准是 **tools\** 目录, 不是项目根 —— paths.PROJ 指的就是 tools\,
# 而那两个脚本(下载守护.py / BBDown-manager.py)和 BBDown.exe 都躺在 tools\ 里。
# 一开始这里写成项目根, 结果测试在测一个错目录(幸好被断言抓住)。
PROJECT = REAL_TOOLS


def load_stop():
    spec = importlib.util.spec_from_file_location("stop_safely_under_test",
                                                  STOP_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _p(*parts):
    return os.path.join(PROJECT, *parts)


class TestProcessMatching(unittest.TestCase):
    """命令行匹配: 只认当前这一份项目, 绝不认别的副本."""

    def setUp(self):
        self.m = load_stop()

    def test_matches_own_project_scripts(self):
        cases = [
            [r"F:\python\python.exe", _p("guard.py")],
            [r"F:\python\python.exe", _p("下载守护.py"), "--status"],
            [r"F:\python\python.exe", _p("BBDown-manager.py"), "--all", "--yes"],
            [_p("BBDown.exe"), "https://www.bilibili.com/video/av1",
             "--work-dir", os.path.join(REAL_ROOT, "videos")],
            # ffmpeg 在 tools\ffmpeg-8.0-full_build\bin\ 下(不是 tools\ 根) ——
            # 这条就是用来钉住路径层级的: 写错层级会认不出该杀的 ffmpeg
            [_p("ffmpeg-8.0-full_build", "bin", "ffmpeg.exe"), "-i", "in.mp4"],
            [_p("ffmpeg-8.0-full_build", "bin", "ffprobe.exe"), "-v", "quiet"],
        ]
        for cmd in cases:
            self.assertTrue(self.m.matches_project(PROJECT, cmd),
                            "应该认出来: %s" % cmd)

    def test_covers_the_real_exe_locations_from_paths(self):
        """匹配表必须以 paths 定义的权威位置兜底(别靠手写层级)."""
        from bbdown_kit import paths
        cmds = [[paths.bbdown_exe(), "av1"], [paths.ffmpeg_exe(), "-i", "x"]]
        for cmd in cmds:
            self.assertTrue(self.m.matches_project(PROJECT, cmd),
                            "paths 给出的位置必须认得出: %s" % cmd)
        # 而且这些路径真的在 tools\ 下
        for real in (paths.bbdown_exe(), paths.ffmpeg_exe()):
            self.assertTrue(real.startswith(REAL_TOOLS),
                            "%s 不在 tools\\ 下, 匹配表要跟着改" % real)

    def test_does_not_match_another_copy_of_the_project(self):
        """回归: 沙盒/备份/第二份安装里的进程, 一条都不能碰."""
        foreign = [
            [r"F:\python\python.exe", r"C:\tmp\sandbox\tools\BBDown-manager.py"],
            [r"F:\python\python.exe", r"C:\tmp\sandbox\tools\下载守护.py"],
            [r"F:\python\python.exe", r"C:\tmp\sandbox\tools\guard.py"],
            [r"D:\备份\BBDown_with_FFmpeg\tools\BBDown.exe", "av1"],
            [r"D:\另一个项目\tools\ffmpeg-8.0-full_build\bin\ffmpeg.exe"],
        ]
        for cmd in foreign:
            self.assertFalse(self.m.matches_project(PROJECT, cmd),
                             "绝不能认成自己的: %s" % cmd)

    def test_bare_filename_is_not_enough(self):
        """光有文件名、没有本项目的路径 -> 不认(这正是当初误杀的原因)."""
        for cmd in ([r"F:\python\python.exe", "BBDown-manager.py", "--all"],
                    [r"F:\python\python.exe", "下载守护.py"],
                    ["BBDown.exe", "av1"]):
            self.assertFalse(self.m.matches_project(PROJECT, cmd),
                             "只有文件名不该认: %s" % cmd)

    def test_forward_slashes_and_case_are_tolerated(self):
        """命令行里的斜杠方向/大小写不固定, 要能容忍."""
        forward = _p("BBDown-manager.py").replace("\\", "/")
        cmd = [r"F:\python\python.exe", forward]
        self.assertTrue(self.m.matches_project(PROJECT, cmd))
        self.assertTrue(self.m.matches_project(PROJECT.lower(), cmd))
        self.assertTrue(self.m.matches_project(PROJECT.upper(), cmd))

    def test_empty_and_garbage_cmdline(self):
        self.assertFalse(self.m.matches_project(PROJECT, None))
        self.assertFalse(self.m.matches_project(PROJECT, []))
        self.assertFalse(self.m.matches_project(PROJECT, [""]))
        self.assertFalse(self.m.matches_project(PROJECT, ["任务管理器"]))
        # 路径是"前缀但不是同一个目录": <项目>_old 不该被认成 <项目>
        self.assertFalse(self.m.matches_project(
            PROJECT, [os.path.join(PROJECT + "_old", "tools", "下载守护.py")]))


class TestCleanLeftovers(TempRootTest):
    """清理部分: 只动该动的, 真实文件与记录一个都不能碰."""

    def setUp(self):
        super().setUp()
        self.m = load_stop()

    def _up(self):
        up = folder("UP主下载", "123_测试UP")
        os.makedirs(up, exist_ok=True)
        return up

    def test_cleans_workdir_tmp_and_ghosts(self):
        up = self._up()
        state = os.path.join(up, "下载状态.json")
        write_state(up,
                    videos=[{"aid": "111", "bvid": "BV1", "title": "有文件"},
                            {"aid": "222", "bvid": "BV2", "title": "幽灵"}],
                    record={"111": {"title": "有文件"}, "222": {"title": "幽灵"}})
        # 真实成片
        with open(os.path.join(up, "有文件_111.mp4"), "wb") as f:
            f.write(b"real")
        # <aid> 临时目录(刚动过, 模拟正在下载被掐断)
        aid = "100000000000002"
        wdir = os.path.join(up, aid)
        os.makedirs(wdir)
        with open(os.path.join(wdir, "%s.P1.999.mp4" % aid), "wb") as f:
            f.write(b"x" * 1000)
        # 半成品 .tmp
        with open(state + ".tmp", "w", encoding="utf-8") as f:
            f.write("{}")

        report = self.m.clean_leftovers()

        self.assertEqual(report["workdirs"], 1)
        self.assertFalse(os.path.exists(wdir), "临时目录该被清掉")
        self.assertFalse(os.path.exists(state + ".tmp"), ".tmp 该被清掉")
        self.assertEqual(report["ghosts"], 1)
        self.assertTrue(os.path.exists(os.path.join(up, "有文件_111.mp4")),
                        "真实成片不能被删")
        import json
        with open(state, encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("111", data["已下载"])
        self.assertNotIn("222", data["已下载"], "幽灵记录该被撤回")
        self.assertEqual(len(data["投稿列表"]), 2, "名单不能被改")

    def test_does_not_touch_a_lookalike_directory(self):
        """名字不是 aid、或里面混着别人文件的目录, 一个都不动."""
        up = self._up()
        write_state(up, videos=[], record={})
        # 1) 不是纯数字目录
        normal = os.path.join(up, "某个标题_123")
        os.makedirs(normal)
        with open(os.path.join(normal, "片子.mp4"), "wb") as f:
            f.write(b"x")
        # 2) 是纯数字, 但里面有"不属于这个 aid"的文件 -> 不是纯临时目录
        mixed = os.path.join(up, "100000000000002")
        os.makedirs(mixed)
        with open(os.path.join(mixed, "别人的标题_999.mp4"), "wb") as f:
            f.write(b"x")
        # 3) 是纯数字, 但有子目录(像多P成片)
        nested = os.path.join(up, "100000000000003")
        os.makedirs(os.path.join(nested, "[P01]第一集"))
        with open(os.path.join(nested, "[P01]第一集", "a.mp4"), "wb") as f:
            f.write(b"x")

        report = self.m.clean_leftovers()

        self.assertEqual(report["workdirs"], 0)
        for path in (normal, mixed, nested):
            self.assertTrue(os.path.exists(path), "不该动 %s" % path)

    def test_running_twice_is_harmless(self):
        up = self._up()
        write_state(up, videos=[{"aid": "1", "bvid": "", "title": "t"}],
                    record={})
        first = self.m.clean_leftovers()
        second = self.m.clean_leftovers()
        self.assertEqual(second["workdirs"], 0)
        self.assertEqual(second["ghosts"], 0)
        self.assertEqual(first["ghosts"], 0)


class TestGracefulDrain(TempRootTest):
    """**核心行为**: 收到停止请求后不再开新任务, 但在下的必须下完.

    这一条如果坏了, 优雅停止就退化成"掐断" —— 那正是它要避免的(留几 GB 半成品)。
    """

    def setUp(self):
        super().setUp()
        from bbdown_kit import lockstep
        self.lockstep = lockstep
        from bbdown_kit import procs
        self.procs = procs
        procs.clear_graceful_stop()

    def tearDown(self):
        self.procs.clear_graceful_stop()

    def test_in_flight_finishes_but_new_ones_are_not_picked_up(self):
        """在下的那个下完; 排队的那个不许开工。"""
        videos = [{"aid": "1", "title": "在下的"},
                  {"aid": "2", "title": "排队等着不该开工"},
                  {"aid": "3", "title": "更不该开工"}]
        started = []
        finished = []
        stop_written = []

        def run_one(video):
            started.append(video["aid"])
            if video["aid"] == "1":
                # 第一个开工之后马上写停止请求: 模拟"用户按了停止"
                self.procs.ask_graceful_stop()
                stop_written.append(True)
                time.sleep(0.3)          # 让它"下完"要花点时间
            return video["aid"]

        def on_result(video, result, done, elapsed):
            finished.append(video["aid"])
            return True

        throttle = self.lockstep.Throttle(1, 0.0, 99, 0)
        ok, failed = self.lockstep.run_batch(
            videos, run_one, on_result, throttle,
            should_stop=self.procs.graceful_stop_requested)

        self.assertEqual(finished, ["1"], "在下的那个必须下完")
        self.assertEqual(started, ["1"], "不该开工新的")
        self.assertEqual((ok, failed), (1, 0))

    def test_without_stop_request_it_runs_everything(self):
        """没收到停止请求时行为不变(不能把正常下载也拦了)."""
        videos = [{"aid": str(i), "title": "t%d" % i} for i in range(5)]
        finished = []

        def run_one(video):
            return video["aid"]

        def on_result(video, result, done, elapsed):
            finished.append(video["aid"])
            return True

        throttle = self.lockstep.Throttle(1, 0.0, 99, 0)
        ok, _failed = self.lockstep.run_batch(videos, run_one, on_result,
                                              throttle,
                                              should_stop=lambda: False)
        self.assertEqual(len(finished), 5)
        self.assertEqual(ok, 5)

    def test_stop_before_start_writes_nothing(self):
        """一开始就要求停: 一个新任务都不许开工(但也不能崩)."""
        videos = [{"aid": "1", "title": "t"}]
        started = []

        def run_one(video):
            started.append(video["aid"])
            return video["aid"]

        def on_result(video, result, done, elapsed):
            return True

        self.procs.ask_graceful_stop()
        throttle = self.lockstep.Throttle(1, 0.0, 99, 0)
        ok, failed = self.lockstep.run_batch(
            videos, run_one, on_result, throttle,
            should_stop=self.procs.graceful_stop_requested)
        self.assertEqual(started, [])
        self.assertEqual((ok, failed), (0, 0))


class TestGracefulRequestFiles(TempRootTest):
    """两个停止请求文件语义不同, 绝不能混用."""

    def test_graceful_and_force_are_separate_files(self):
        from bbdown_kit import paths, procs
        self.assertNotEqual(paths.stop_request(), paths.graceful_stop_request())
        self.assertFalse(procs.graceful_stop_requested())
        self.assertTrue(procs.ask_graceful_stop())
        self.assertTrue(procs.graceful_stop_requested())
        # 重要的是: 写了优雅停止, **不能**让"立刻让位"也变成真
        self.assertFalse(procs.stop_requested_for_me(),
                         "优雅停止不该被当成强制让位")
        self.assertTrue(procs.clear_graceful_stop())
        self.assertFalse(procs.graceful_stop_requested())

    def test_is_idempotent(self):
        from bbdown_kit import procs
        self.assertTrue(procs.ask_graceful_stop())
        self.assertTrue(procs.ask_graceful_stop())
        self.assertTrue(procs.graceful_stop_requested())
        procs.clear_graceful_stop()
        self.assertFalse(procs.clear_graceful_stop(), "重复清不算成功")


class TestGuardHonoursGracefulStop(TempRootTest):
    """守护必须把"优雅停止"当成停止信号上报.

    这一条踩过坑: 管理器那半边完全正确(不再开新任务、把在下的下完), 但守护
    没被告知 —— 它看到"一轮正常结束"就接着开了下一轮, 用户以为停了它却还在下。
    日志里长这样:
        第 1 轮结束: 成功 3, 缺口 1 -> 350
        第 2 轮前统计: 缺口 350 个        <- 不该发生
    """

    def setUp(self):
        super().setUp()
        from bbdown_kit import procs
        self.procs = procs
        procs.clear_graceful_stop()
        self.addCleanup(procs.clear_graceful_stop)

    def _guard(self):
        from types import SimpleNamespace

        from support import REAL_TOOLS
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "guard_graceful", os.path.join(REAL_TOOLS, "下载守护.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        args = SimpleNamespace(order=None, no_takeover=True, force=False,
                               max_rounds=1, forever=False)
        settings = {"parallel": 1, "interval": 0,
                    "download_timeout_minutes": 30,
                    "order": "collection_first", "data_root": ""}
        return module.Guard(args, settings, {}, self.tmp)

    def test_graceful_request_is_reported_as_stop(self):
        guard = self._guard()
        self.assertFalse(guard.stop_signal(), "没有请求时不该说停")
        self.procs.ask_graceful_stop()
        self.assertTrue(guard.stop_signal(),
                        "优雅停止请求必须被守护当成停止信号")

    def test_force_stop_request_still_works(self):
        guard = self._guard()
        with open(paths.stop_request(), "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        self.assertTrue(guard.stop_signal(), "让位请求仍然要生效")

    def test_graceful_request_means_long_grace_period(self):
        """守护传给 runner 的宽限期不能是 None(那是"立刻掐"的语义)."""
        src = open(os.path.join(REAL_TOOLS, "下载守护.py"),
                   encoding="utf-8").read()
        self.assertIn("grace_seconds=", src)
        self.assertNotIn("grace_seconds=None", src,
                         "守护不该用 None: 那会立刻掐掉在下的")

    def test_guard_clears_a_stale_request_on_start(self):
        """重新启动守护 = 我要继续下, 所以启动时要撤销上次的停止请求."""
        src = open(os.path.join(REAL_TOOLS, "下载守护.py"),
                   encoding="utf-8").read()
        self.assertIn("clear_graceful_stop()", src)


class TestStaleBoundary(TempRootTest):
    """stale_seconds=0 的边界: 文件比 now 还新一点点也必须算"可以清".

    这是踩过的一个真 bug, 而且**时灵时不灵**: 判定写成 `now - newest < 0`
    时, 时钟精度只有毫秒级, `now` 完全可能比刚写下的 mtime 早 0.001 秒 ——
    于是"掐断之后立刻清理"这条路径偶发失效(实测 200 次里失败十几次)。
    """

    def test_file_newer_than_now_still_cleanable(self):
        from bbdown_kit import workdirs

        aid = "100000000000002"
        up = self.up_folder()
        wdir = os.path.join(up, aid)
        os.makedirs(wdir)
        target = os.path.join(wdir, "%s.P1.999.mp4" % aid)
        with open(target, "wb") as f:
            f.write(b"x" * 100)

        # 故意把 now 取得比文件时间早(模拟时钟精度带来的 -0.001 秒)
        now = time.time() - 0.001
        ok, why = workdirs.cleanable(wdir, aid, now=now, stale_seconds=0)
        self.assertTrue(ok, "stale_seconds=0 时必须能清, 不该被 %r 挡住" % why)

    def test_positive_stale_still_protects_a_fresh_dir(self):
        """冷静期大于 0 时, 刚动过的目录仍然要保护住(这条不能修坏)."""
        from bbdown_kit import workdirs

        aid = "100000000000002"
        up = self.up_folder()
        wdir = os.path.join(up, aid)
        os.makedirs(wdir)
        with open(os.path.join(wdir, "%s.P1.999.mp4" % aid), "wb") as f:
            f.write(b"x" * 100)

        ok, why = workdirs.cleanable(wdir, aid, stale_seconds=600)
        self.assertFalse(ok, "刚动过的目录必须保护住")
        self.assertIn("正在下载", why)

    def test_old_dir_is_cleanable_with_normal_setting(self):
        from bbdown_kit import workdirs

        aid = "100000000000002"
        up = self.up_folder()
        wdir = os.path.join(up, aid)
        os.makedirs(wdir)
        target = os.path.join(wdir, "%s.P1.999.mp4" % aid)
        with open(target, "wb") as f:
            f.write(b"x" * 100)
        old = time.time() - 3600
        os.utime(target, (old, old))

        ok, _why = workdirs.cleanable(wdir, aid, stale_seconds=600)
        self.assertTrue(ok, "一小时没动的残留应该可以清")


class TestStopGating(unittest.TestCase):
    """强杀路径的铁律: 还有进程没死时, 一个文件都不许删."""

    def setUp(self):
        self.src = open(STOP_PY, encoding="utf-8").read()

    def test_force_path_gates_cleanup_on_no_processes(self):
        start = self.src.index("def _force_stop(")
        end = self.src.index("if __name__ ==", start)
        body = self.src[start:end]
        self.assertIn("if left:", body, "必须先判断还有没有进程")
        # 提前返回必须出现在 clean_leftovers 之前
        self.assertLess(body.index("if left:"), body.index("clean_leftovers()"),
                        "有残留进程时要提前返回, 不能继续删文件")
        self.assertIn("return 2", body)

    def test_graceful_is_the_default_path(self):
        """默认必须是"请求 + 等待", 而不是强杀."""
        start = self.src.index("def main(")
        body = self.src[start:]
        graceful = body.index("procs.ask_graceful_stop()")
        force_call = body.index("_force_stop(args)", graceful)
        self.assertLess(graceful, force_call,
                        "默认路径要先发优雅停止请求")
        # 而且只有超时/--force 才走强杀
        self.assertIn("if args.force:", body)
        self.assertIn("if left:", body)

    def test_dry_run_never_touches_anything(self):
        start = self.src.index("if args.dry_run:")
        end = self.src.index("if not victims:", start)
        block = self.src[start:end]
        for forbidden in ("stop_everything(", "clean_leftovers(", "_kill_pid(",
                          "ask_graceful_stop("):
            self.assertNotIn(forbidden, block,
                             "干跑分支里不能调用 %s" % forbidden)


if __name__ == "__main__":
    unittest.main()
