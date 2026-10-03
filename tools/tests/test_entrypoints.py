# -*- coding: utf-8 -*-
"""两个入口的"加载契约"测试.

为什么单独测这个: 入口是薄启动器, 真正的逻辑都在 bbdown_kit 里, 所以单元测试
很难覆盖到它们 —— 结果就是"包里的模块都好好的, 但双击 .bat 起不来"(踩过:
入口 import 了一个已经搬走的函数)。这里把两个入口真的 import 一遍, 并且
核对它们暴露的名字和命令行参数没变。
"""

import importlib.util
import os
import sys
import types
import unittest
from contextlib import contextmanager

from support import REAL_TOOLS, RealProjectTest, paths


class _FakeMsvcrt(object):
    """把预设的按键喂给 read_key, 好离线验证按键映射."""

    def __init__(self, keys):
        self.queue = list(keys)

    def getwch(self):
        if not self.queue:
            raise EOFError("没有更多按键了")
        return self.queue.pop(0)


@contextmanager
def _fake_msvcrt(fake):
    """临时把 sys.modules 里的 msvcrt 换成假的.

    read_key 里是 `import msvcrt`(函数内导入), 所以换掉 sys.modules 就够了。
    """
    saved = sys.modules.get("msvcrt")
    sys.modules["msvcrt"] = fake
    try:
        yield fake
    finally:
        if saved is None:
            sys.modules.pop("msvcrt", None)
        else:
            sys.modules["msvcrt"] = saved


def load_entry(filename):
    """按路径加载入口模块(文件名是中文/带横线, 不能直接 import)."""
    from support import REAL_TOOLS

    path = os.path.join(REAL_TOOLS, filename)
    spec = importlib.util.spec_from_file_location(
        "entry_%s" % abs(hash(filename)), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestGuardEntry(RealProjectTest):
    """下载守护.py."""

    def test_imports_and_exposes_main(self):
        guard = load_entry("下载守护.py")
        self.assertTrue(callable(guard.main))
        for name in ("build_parser", "Guard", "write_status", "rotate_guard_log",
                     "prune_run_dirs", "sleep_with_status", "yield_to_new_guard",
                     "take_over", "take_manager_lock"):
            self.assertTrue(hasattr(guard, name), "守护缺少 %s" % name)

    def test_cli_flags_are_unchanged(self):
        """这些参数是给 .bat 和定时任务用的, 少一个就会静默地改变行为."""
        guard = load_entry("下载守护.py")
        parser = guard.build_parser()
        opts = set()
        for action in parser._actions:
            opts.update(action.option_strings)
        expected = {"--status", "--audit", "--order", "--probe-login",
                    "--probe-api", "--probe-aids", "--no-takeover", "--force",
                    "--max-rounds", "--forever", "--help"}
        self.assertEqual(expected - opts, set(), "守护少了参数")

    def test_rest_and_speed_tables_are_intact(self):
        """休息档位: 5 分钟起步, 加到 20 分钟就停(不再无限等).

        这是按用户要求定的: "5分钟一路加长到20分钟, 如果20分钟还不行,
        那就优雅结束"。所以这里连"档位数量"一起钉住 —— 档位用完就该收工,
        加档位等于把"无限等"又请回来了。
        """
        guard = load_entry("下载守护.py")
        self.assertEqual(guard.REST_STEPS, [300, 600, 1200])
        self.assertEqual(max(guard.REST_STEPS), 1200,
                         "最久只等 20 分钟")
        self.assertEqual(guard.SPEED_LEVELS,
                         [(3, 2.0), (2, 3.0), (1, 5.0), (1, 8.0)])
        self.assertEqual(guard.STALL_ROUNDS, 4)
        self.assertEqual(guard.KEEP_RUN_DIRS, 10)

    def test_rest_budget_runs_out(self):
        """"档位用完"要真的能触发 —— 以前下标被夹住, 这个条件永远不成立."""
        guard = load_entry("下载守护.py")
        from types import SimpleNamespace
        g = guard.Guard(SimpleNamespace(order=None), {"parallel": 3,
                                                      "interval": 2.0}, {}, self.tmp)
        self.assertFalse(g.rest_budget_used_up(), "还没休息过就不该收工")
        for _ in range(len(guard.REST_STEPS)):
            g.rest_index += 1
        self.assertFalse(g.rest_budget_used_up(),
                         "刚等完最久那一档, 还要再给一次机会")
        g.rest_index += 1
        self.assertTrue(g.rest_budget_used_up(),
                        "最久那一档也等过了还不通, 就该收工")

    def test_manager_command_has_the_guard_contract(self):
        """守护拉起管理器时带的参数就是两边约定的接口, 不能少."""
        guard = load_entry("下载守护.py")
        from types import SimpleNamespace
        g = guard.Guard(SimpleNamespace(order=None), {"parallel": 3,
                                                      "interval": 2.0}, {}, self.tmp)
        cmd = g.manager_command(3, 2.0, os.path.join(self.tmp, "ev.jsonl"))
        for flag in ("--all", "--yes", "--lock-held", "--event-log",
                     "--pause-seconds", "--parallel",
                     "--interval"):
            self.assertIn(flag, cmd, "守护没给管理器传 %s" % flag)
        self.assertEqual(cmd[1], paths.manager_script())
        # 守护**不该**再关掉管理器自带的限流保护: 以前写死 --fail-threshold 99,
        # 于是管理器那套"降速 + 冷却"从不生效, 只剩守护每 40 秒掐一次进程。
        self.assertNotIn("--fail-threshold", cmd,
                         "限流保护要留给管理器自己用(默认阈值), 别关掉它")


class TestManagerEntry(RealProjectTest):
    """BBDown-manager.py."""

    def test_imports_and_exposes_main(self):
        manager = load_entry("BBDown-manager.py")
        self.assertTrue(callable(manager.main))
        for name in ("run_manager", "collect_tasks", "run_tasks", "build_parser",
                     "apply_settings", "select_up_folders",
                     "select_folders_input", "confirm_download",
                     "process_single_video"):
            self.assertTrue(hasattr(manager, name), "管理器缺少 %s" % name)

    def test_cli_flags_are_unchanged(self):
        manager = load_entry("BBDown-manager.py")
        parser = manager.build_parser()
        opts = set()
        for action in parser._actions:
            opts.update(action.option_strings)
        expected = {"--url", "--video", "--collection", "--limit", "--parallel",
                    "--interval", "--fail-threshold", "--pause-seconds", "--all",
                    "--order", "--backfill", "--retry-skip", "--full",
                    "--full-days", "--bbdown-args", "--yes", "--event-log",
                    "--lock-held", "--download-timeout"}
        self.assertEqual(expected - opts, set(), "管理器少了参数")

    def test_menu_keys_are_all_handled(self):
        """菜单上写出来的按键必须都有人处理(写了不认就是骗人)."""
        manager = load_entry("BBDown-manager.py")
        listed = {key for key, _what in manager.MENU_ACTIONS}
        handled = {"a", "n", "s", "c", "r", "f", "d", "q"}
        self.assertEqual(listed, {k.upper() for k in handled})

    def test_menu_freeze_key_matches_the_handler(self):
        """菜单里那个"冻结/启用"的键, 真按下去要有人接."""
        manager = load_entry("BBDown-manager.py")
        src = open(os.path.join(REAL_TOOLS, "BBDown-manager.py"),
                   encoding="utf-8").read()
        self.assertIn('elif key == "d":', src,
                      "菜单列了 D 但按键分支里没有它")

    def test_menu_window_keeps_the_cursor_visible(self):
        """回归: 光标必须永远在显示范围内.

        以前的菜单一次性打印**所有** UP主(155 行), 控制台一滚到底, 而光标行
        在列表顶上、早就出了屏幕 —— 用户看到的是"按什么键都跳到最底下、
        光标根本不在"。所以这条要钉死: 无论光标在哪, 它都得在窗口里。
        """
        manager = load_entry("BBDown-manager.py")
        folders = ["%d_x" % i for i in range(155)]
        for cursor in list(range(0, 155, 7)) + [0, 1, 153, 154]:
            start, end = manager.menu_window(folders, cursor, set())
            self.assertTrue(start <= cursor < end,
                            "光标 %d 掉出窗口 [%d, %d)" % (cursor, start, end))
            self.assertGreater(end - start, 0)
            self.assertLessEqual(end - start, len(folders))

    def test_menu_window_handles_small_and_empty_lists(self):
        manager = load_entry("BBDown-manager.py")
        self.assertEqual(manager.menu_window([], 0, set()), (0, 0))
        self.assertEqual(manager.menu_window(["a"], 0, set()), (0, 1))
        self.assertEqual(manager.menu_window(["a", "b", "c"], 2, set()), (0, 3))

    def test_menu_window_never_exceeds_a_screen(self):
        """一屏装不下时不能硬塞 —— 那正是"滚到看不见光标"的成因."""
        manager = load_entry("BBDown-manager.py")
        folders = ["%d_x" % i for i in range(500)]
        start, end = manager.menu_window(folders, 250, set())
        rows = manager.console_rows()
        # 窗口 + 菜单本身占的行, 不该超过终端高度太多(留一点余量)
        self.assertLessEqual(end - start + len(manager.MENU_ACTIONS) + 9, rows + 2)

    def test_navigation_keys_are_mapped(self):
        """↑↓ / PgUp / PgDn / Home / End 都要认(两套前缀都试)."""
        manager = load_entry("BBDown-manager.py")
        expected = {
            ("\xe0", "H"): "up", ("\x00", "H"): "up",
            ("\xe0", "P"): "down", ("\x00", "P"): "down",
            ("\xe0", "I"): "pageup", ("\xe0", "Q"): "pagedown",
            ("\xe0", "G"): "home", ("\xe0", "O"): "end",
        }
        for keys, want in expected.items():
            fake = _FakeMsvcrt(list(keys))
            with _fake_msvcrt(fake):
                got = manager.read_key()
            self.assertEqual(got, want, "%r 应映射成 %s" % (keys, want))

    def test_escape_is_not_treated_as_a_letter(self):
        manager = load_entry("BBDown-manager.py")
        with _fake_msvcrt(_FakeMsvcrt(["\x1b"])):
            self.assertIsNone(manager.read_key())

    def test_settings_precedence(self):
        """命令行 > 下载设置.txt > 默认值."""
        manager = load_entry("BBDown-manager.py")
        from types import SimpleNamespace

        class Args(SimpleNamespace):
            pass

        settings = {"parallel": 3, "interval": 2.0, "fail_threshold": 10,
                    "pause_seconds": 30, "full_days": 7,
                    "download_timeout_minutes": 30, "order": "collection_first",
                    "data_root": ""}
        args = Args(parallel=None, interval=None, fail_threshold=None,
                    pause_seconds=None, full_days=None, download_timeout=None,
                    order=None)
        manager.apply_settings(args, settings)
        self.assertEqual(args.parallel, 3)
        self.assertEqual(args.interval, 2.0)
        self.assertEqual(args.full_days, 7)
        self.assertEqual(args.download_timeout, 1800)      # 30 分钟 -> 秒
        self.assertEqual(args.order, "collection_first")

        args = Args(parallel=1, interval=0.5, fail_threshold=99,
                    pause_seconds=0, full_days=0, download_timeout=0, order="largest")
        manager.apply_settings(args, settings)
        self.assertEqual(args.parallel, 1)
        self.assertEqual(args.interval, 0.5)
        self.assertEqual(args.fail_threshold, 99)
        self.assertEqual(args.full_days, 0)
        self.assertIsNone(args.download_timeout)           # 0 = 不限时
        self.assertEqual(args.order, "largest")

    def test_ascii_guard_shim_points_at_the_real_script(self):
        """tools\\guard.py 是给 .bat 用的 ASCII 入口, 不能指向不存在的文件."""
        self.assertTrue(os.path.exists(self.real_path("guard.py")))
        self.assertIn("下载守护.py", self.read_text("guard.py"))
        self.assertTrue(os.path.exists(self.real_path("下载守护.py")))


class TestBatchFiles(RealProjectTest):
    """几个 .bat 必须还在指向真实存在的脚本(改了入口就会悄悄失效)."""

    def read(self, name):
        return self.read_text(name)

    def test_batch_files_reference_existing_scripts(self):
        for bat, script in (("启动下载守护.bat", "guard.py"),
                            ("下载管理器.bat", "BBDown-manager.py"),
                            ("定时更新全部博主.bat", "BBDown-manager.py"),
                            ("BBDown-身份登录.bat", "BBDown-login.py"),
                            ("运行测试.bat", "tests")):
            text = self.read(bat)
            self.assertIn(script, text, "%s 没引用 %s" % (bat, script))
            self.assertTrue(
                os.path.exists(self.real_path(script)),
                "%s 引用的 %s 不存在" % (bat, script))

    def test_python_env_var_is_never_passed_to_call(self):
        """BBDOWN_PYTHON 的值绝不能当 call 的参数 —— call 会把整行重新解析.

        这样一来值里的一个双引号就能结束引号, 剩下的部分被当成命令执行
        (环境变量 -> 任意命令执行)。现在先把值读进变量, 再交给 :trypy_var,
        由它用 !VAR! 读; cmd 是先解析后展开, 值里的引号和 & 不再有含义。
        """
        text = self.read("find_python.bat")
        for bad in ('call :trypy "%BBDOWN_PYTHON%"',
                    'call :trypy "!BBDOWN_PYTHON!"'):
            self.assertNotIn(bad, text,
                             "又用 call 直接传环境变量了: %s" % bad)
        self.assertIn("call :trypy_var", text)

    def test_candidate_check_demands_a_chosen_exit_code(self):
        """只认"退出码 0"是不够的: 有些程序给什么参数都返回 0.

        (实测: attrib.exe 改名成 python.exe + 一个假 python3.dll 就能骗过
        结构检查。) 所以要求候选真的跑 python 代码并返回约定好的 37。
        """
        text = self.read("find_python.bat")
        self.assertIn("sys.exit(37)", text)
        self.assertIn("if not errorlevel 37", text)
        self.assertIn("if errorlevel 38", text)
        # for /f 抓 stdout 的写法在这个环境下什么都抓不到(实测), 别再回到它
        self.assertNotIn("PYTOKEN", text)

    def test_batch_files_stay_ascii(self):
        """cmd.exe 解析含非 ASCII 字节的 .bat 会出错, 这几个文件必须全 ASCII.

        (中文提示语由 python 那边打印, 所以 .bat 里不需要中文。)
        """
        for name in ("启动下载守护.bat", "下载管理器.bat",
                     "定时更新全部博主.bat", "运行测试.bat", "find_python.bat",
                     "BBDown-身份登录.bat"):
            raw = self.read_bytes(name)
            bad = [(i, b) for i, b in enumerate(raw) if b > 0x7f]
            self.assertEqual(bad, [], "%s 含非 ASCII 字节: %s" % (name, bad[:5]))


if __name__ == "__main__":
    unittest.main()
