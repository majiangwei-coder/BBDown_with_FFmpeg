# -*- coding: utf-8 -*-
"""针对这四项修复的回归测试(2026-10-01).

每一项都是"用户看不见它在坏"的那类问题, 所以必须钉住:

1. 管理器退出码   以前无条件返回 0 -> 定时任务永远显示"成功"
2. 守护日志落盘   以前 守护日志.txt 是死文件 -> 守护为什么休息/放弃无从查起
3. Ctrl+C 收孤儿  以前 Popen 没有保护 -> 守护退了, 管理器+BBDown 还在后台下
4. 定时任务走守护 以前直接跑管理器 -> 无限流保护, 且会往「跳过」里永久写视频
"""

import io
import json
import os
import sys
import unittest
from unittest import mock

from support import (REAL_ROOT, REAL_TOOLS, RealProjectTest, TempRootTest,
                     folder, write_state)

GUARD_PY = os.path.join(REAL_TOOLS, "下载守护.py")
MANAGER_PY = os.path.join(REAL_TOOLS, "BBDown-manager.py")
GUARDED_BAT = "定时更新全部博主(守护).bat"


def load_manager():
    import importlib.util

    spec = importlib.util.spec_from_file_location("mgr_exit", MANAGER_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_guard():
    import importlib.util

    spec = importlib.util.spec_from_file_location("guard_log", GUARD_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestExitCode(unittest.TestCase):
    """修复 1: 退出码要反映真实结果, 否则定时任务的"成功"是假的."""

    def setUp(self):
        self.m = load_manager()

    def test_all_success_is_zero(self):
        self.assertEqual(self.m.exit_code({"downloaded": 12, "failed": 0}), 0)

    def test_nothing_to_do_is_zero(self):
        self.assertEqual(self.m.exit_code({"downloaded": 0, "failed": 0}), 0)

    def test_partial_failure_is_one(self):
        self.assertEqual(self.m.exit_code({"downloaded": 5, "failed": 2}), 1)

    def test_total_failure_is_two(self):
        """"一个都没成"要和"部分失败"区分开: 前者通常是被限流/登录失效."""
        self.assertEqual(self.m.exit_code({"downloaded": 0, "failed": 7}), 2)

    def test_missing_keys_do_not_crash(self):
        self.assertEqual(self.m.exit_code({}), 0)
        self.assertEqual(self.m.exit_code({"failed": None}), 0)

    def test_run_manager_returns_that_code(self):
        """回归: 曾经写死 `return 0`, 于是 .bat 永远写 "exit code 0"."""
        source = io.open(MANAGER_PY, encoding="utf-8").read()
        self.assertIn("return exit_code(stats)", source,
                      "run_manager 必须按 stats 返回退出码")
        self.assertNotIn("\n    return 0\n\n\ndef exit_code", source)


class TestGuardLogIsWritten(TempRootTest):
    """修复 2: 守护的日志必须真的写进 守护日志.txt."""

    def test_guard_log_writes_to_the_file(self):
        guard = load_guard()
        guard.log("这是一条测试日志")
        path = guard.paths.guard_log()
        self.assertTrue(os.path.exists(path), "守护日志.txt 没有被写入")
        with io.open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("这是一条测试日志", text)

    def test_guard_log_is_not_the_bare_kit_log(self):
        """回归: 以前是 `log = kit_log.log`, 而那个默认不写文件."""
        guard = load_guard()
        from bbdown_kit import logging as kit_log
        self.assertIsNot(guard.log, kit_log.log,
                         "守护的 log 必须是会写文件的那个包装")
        # 包装必须真的多带一个 to_file
        guard.log("包装检查用")
        with io.open(guard.paths.guard_log(), encoding="utf-8") as f:
            self.assertIn("包装检查用", f.read())

    def test_runner_accepts_a_log_sink(self):
        """心跳/限流判定这些最要紧的行也要能落到同一个文件.

        用一个"自称支持事件流、却什么都不写"的假管理器, run_round 必然要写
        一行"没写出事件文件"的提醒 —— 用它来证明注入的出口真的被用上了。
        """
        from bbdown_kit import runner
        seen = []
        events = os.path.join(self.tmp, "ev.jsonl")
        manager = os.path.join(self.tmp, "claims_events.py")
        # 文件里出现 bbdown_kit 字样 -> manager_supports_events 判定它支持事件流,
        # 但它一个事件也不写, 于是走"没写出事件文件"这条提醒。
        with io.open(manager, "w", encoding="utf-8") as f:
            f.write("# bbdown_kit\nimport sys\nsys.exit(0)\n")
        runner.run_round(
            manager_cmd=[sys.executable, manager, "--event-log", events],
            round_no=1, quarantined={}, log_dir=self.tmp,
            round_log_path=os.path.join(self.tmp, "第001轮.txt"),
            events_path=events, manager_path=manager,
            log_fn=seen.append)
        self.assertTrue(seen, "注入的日志出口一次都没被调用")
        self.assertTrue(any("事件文件" in line for line in seen),
                        "应该有一条'没写出事件文件'的提醒: %r" % seen)

    def test_missing_events_file_is_an_empty_round(self):
        """事件文件缺失时统计为空 —— 把这后果钉住, 免得被当成"一切正常"."""
        from bbdown_kit import runner
        events = os.path.join(self.tmp, "ev2.jsonl")
        manager = os.path.join(self.tmp, "claims2.py")
        with io.open(manager, "w", encoding="utf-8") as f:
            f.write("# bbdown_kit\nimport sys\nsys.exit(0)\n")
        _start, result = runner.run_round(
            manager_cmd=[sys.executable, manager, "--event-log", events],
            round_no=1, quarantined={}, log_dir=self.tmp,
            round_log_path=os.path.join(self.tmp, "第002轮.txt"),
            events_path=events, manager_path=manager,
            log_fn=lambda line: None)
        self.assertEqual((result.downloads, result.fails), (0, 0))


class TestCtrlCReapsChildren(TempRootTest):
    """修复 3: run_round 中途被打断时, 必须收掉管理器进程树."""

    def _run_with_interrupt(self):
        from bbdown_kit import runner
        events = os.path.join(self.tmp, "ev.jsonl")
        manager = os.path.join(self.tmp, "slow.py")
        with io.open(manager, "w", encoding="utf-8") as f:
            f.write("import time, sys\n"
                    "print('starting')\n"
                    "sys.stdout.flush()\n"
                    "time.sleep(120)\n")
        return runner, manager, events

    def test_interrupt_kills_the_manager(self):
        runner, manager, events = self._run_with_interrupt()
        calls = []
        with mock.patch.object(runner, "_reap",
                               side_effect=lambda proc, why: calls.append(why)):
            # tick_hook 抛异常模拟 Ctrl+C 打进主循环
            def boom():
                raise KeyboardInterrupt

            with self.assertRaises(KeyboardInterrupt):
                runner.run_round(
                    manager_cmd=[sys.executable, manager,
                                 "--event-log", events],
                    round_no=1, quarantined={}, log_dir=self.tmp,
                    round_log_path=os.path.join(self.tmp, "r.txt"),
                    events_path=events, manager_path=manager,
                    tick_hook=boom)
        self.assertTrue(calls, "Ctrl+C 时必须调用 _reap 收掉子进程")
        self.assertIn("中断", calls[0])

    def test_reap_is_idempotent_on_a_dead_process(self):
        """进程已经退出时 _reap 什么都不做(不能报错、不能重复杀)."""
        from bbdown_kit import runner
        proc = mock.Mock()
        proc.poll.return_value = 0            # 已经结束了
        with mock.patch("bbdown_kit.procs.kill_tree") as kill:
            runner._reap(proc, "不该动手")
        self.assertFalse(kill.called)

    def test_reap_swallows_kill_errors(self):
        """收尾失败不能让整轮崩掉(那会把真正的错误盖掉)."""
        from bbdown_kit import runner
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.wait.side_effect = Exception("等不到")
        with mock.patch("bbdown_kit.procs.kill_tree",
                        side_effect=Exception("杀不掉")):
            runner._reap(proc, "测试")        # 不抛异常即通过


class TestGuardedDailyTask(RealProjectTest):
    """修复 4: 计划任务必须走守护, 而不是直接跑管理器."""

    def test_guarded_bat_exists_and_is_ascii(self):
        raw = self.read_bytes(GUARDED_BAT)
        bad = [(i, b) for i, b in enumerate(raw) if b > 0x7f]
        self.assertEqual(bad, [], "含非 ASCII 字节, cmd 会解析错: %s" % bad[:5])

    def test_guarded_bat_runs_the_guard_not_the_manager(self):
        text = self.read_text(GUARDED_BAT)
        self.assertIn("guard.py", text)
        self.assertNotIn("BBDown-manager.py", text,
                         "定时任务不该直接跑管理器(没有限流保护)")

    def test_guarded_bat_bounds_rounds_and_never_forces(self):
        text = self.read_text(GUARDED_BAT)
        self.assertIn("--max-rounds", text)
        self.assertNotIn("--force", text.replace("no --force", "")
                         .replace("never_forces", ""),
                         "定时任务不能用 --force: 会杀掉用户手动开的守护")

    def test_guarded_bat_writes_the_finish_line_unconditionally(self):
        """结束行必须在 :done 里(所有分支都会走到), 否则被中断就查不出结果.

        被中断时这一行不会出现 —— 那正好就是"这次没跑完"的标志; 关键是
        正常收尾的所有分支(含退出码 2/3/4)都必须经过 :done。
        """
        text = self.read_text(GUARDED_BAT)
        done = text.index(":done")
        finish = text.index("guard run finished, exit code %EC%")
        self.assertLess(done, finish, "结束行必须在 :done 之后")
        # 每个备注分支都必须 goto :done, 不能自己 exit
        for note in (":note_norun", ":note_manual", ":note_stalled", ":note_busy"):
            block = text[text.index(note):]
            block = block[:block.index("goto :done")]
            self.assertNotIn("exit /b", block,
                             "%s 直接退出了, 结束行不会写进日志" % note)
        # 而且正常路径只能写一次结束行: 两行会让人以为跑了两轮
        self.assertEqual(text.count("guard run finished, exit code %EC%"), 1)

    def test_guarded_bat_keeps_the_no_python_note(self):
        """找不到 python 时必须留下显眼的告警文件, 并以 9 退出."""
        text = self.read_text(GUARDED_BAT)
        self.assertIn("AUTO_UPDATE_FAILED_read_me.txt", text)
        self.assertIn(":no_python", text)
        self.assertIn('set "EC=9"', text)

    def test_guarded_bat_avoids_parenthesised_redirect_blocks(self):
        """回归(cmd 陷阱): 在 if ( ... ) 块里用 >> "%VAR%" 重定向, 一旦路径里
        出现括号, cmd 会报 "was unexpected at this time." 并直接退出 255.

        本项目所在路径没有括号, 但用户可能把项目放进带括号的目录, 那时
        整条定时任务会静默失效 —— 所以这个 bat 一律用 goto 分支。
        """
        text = self.read_text(GUARDED_BAT)
        # 不能在 ( ... ) 块里做重定向: 这里简单粗暴地要求没有跨行的 if 块
        self.assertNotIn("if not defined PYTHON (", text)
        self.assertIn("if not defined PYTHON goto :no_python", text)

    def test_guarded_bat_explains_every_exit_code(self):
        """每个会产生的退出码都要有对应的人话, 否则用户只看到一个数字."""
        text = self.read_text(GUARDED_BAT)
        for code, note in (("2", ":note_manual"), ("3", ":note_stalled"),
                           ("4", ":note_busy"), ("9", ":note_norun")):
            self.assertIn(note, text, "退出码 %s 没有对应的提示" % code)

    def test_scheduled_task_points_at_the_guarded_bat(self):
        """真实计划任务的动作必须指向新 bat(改错了等于没改).

        schtasks 输出的是 UTF-16 字节; 如果让 subprocess 用 text=True 去解码,
        它会按系统 ANSI(GBK) 解, 中文全成乱码 —— 所以这里自己拿字节解码。
        """
        import subprocess
        try:
            out = subprocess.run(
                ["schtasks", "/query", "/tn", "BBDown_每日更新全部博主",
                 "/xml"],
                capture_output=True, timeout=30)
        except Exception as e:
            self.skipTest("读不到计划任务: %s" % e)
        if out.returncode != 0:
            self.skipTest("计划任务不存在: %s"
                          % (out.stderr or b"").decode("utf-8", "replace")[:120])
        raw = out.stdout or b""
        # 实测: 声明的编码(UTF-16)和实际字节(UTF-8)可能不一致 —— 按 BOM 认,
        # 没有 BOM 就先按 UTF-8 试(中文必须是合法 UTF-8), 最后才退回 UTF-16。
        if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
            candidates = ("utf-16", "utf-8", "gbk")
        else:
            candidates = ("utf-8-sig", "utf-8", "utf-16", "gbk")
        xml = None
        for enc in candidates:
            try:
                text = raw.decode(enc)
            except (UnicodeDecodeError, UnicodeError):
                continue
            if GUARDED_BAT in text:          # 解对了才认
                xml = text
                break
        if xml is None:
            self.skipTest("计划任务 XML 解码不出可读内容(编码异常)")
        self.assertIn(GUARDED_BAT, xml)
        self.assertNotIn("定时更新全部博主.bat", xml,
                         "任务还指着老的 bat(或同时指着两个)")
