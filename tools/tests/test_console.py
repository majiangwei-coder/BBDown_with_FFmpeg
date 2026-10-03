# -*- coding: utf-8 -*-
"""控制台画面: 原地重写, 不闪.

为什么专门测这个:
    菜单以前每按一个键就 `os.system("cls")` 再逐行 print —— cls 先把整个屏幕
    擦白, 再一行行打上去, 中间那张白脸就是闪烁。屏幕越大越明显。
    改成"原地重写"之后, 有两条容易写错的细节必须有测试盯着:
        · 不擦屏, 所以**新画面短了必须把多出来的行盖掉**, 否则旧选项留在屏幕上
        · 写画面时要藏光标, 否则光标在画面上乱跳
"""

import os
import sys
import unittest
from unittest import mock

from support import TempRootTest, folder, write_state


class TestScreenFrame(TempRootTest):
    """Screen 的组帧/落笔."""

    def _screen(self):
        from bbdown_kit import console
        s = console.Screen(stream=_Collector())
        s.enabled = True
        # 不真的去碰控制台, 只记录"它想写什么"。返回 True = 假装写成功了。
        s._move_home = lambda: None
        s._cursor = lambda visible: None
        s._write = lambda text: (s.stream.chunks.append(text), True)[1]
        return s

    def test_frame_lines_are_written_in_one_go(self):
        s = self._screen()
        s.begin()
        s.line("甲")
        s.line("乙")
        s.end()
        self.assertEqual(len(s.stream.chunks), 1,
                         "整帧要一次性写出, 分多次写会看到逐行刷")
        self.assertIn("甲", s.stream.chunks[0])
        self.assertIn("乙", s.stream.chunks[0])

    def test_short_frame_erases_the_extra_lines(self):
        """不擦屏就必须自己盖掉多出来的行, 否则旧内容留在屏幕上."""
        s = self._screen()
        s.begin()
        s.line("第一帧")
        s.line("这一行待会儿要消失")
        s.line("还有这一行")
        s.end()
        s.begin()
        s.line("第二帧")
        s.end()
        text = s.stream.chunks[-1]
        # 第二帧应该仍然是 3 行(补了 2 个空行), 把旧内容盖掉
        self.assertEqual(text.count("\r\n") + 1, 3,
                         "新画面比旧的短, 必须补空行盖掉残影")
        self.assertNotIn("待会儿要消失", text)
        self.assertEqual(s.prev_count, 3)

    def test_frames_never_get_shorter(self):
        """连续变化时, 写出量只增不减(短的那次会被补齐)."""
        s = self._screen()
        for n in (5, 2, 7, 1):
            s.begin()
            for i in range(n):
                s.line("行 %d" % i)
            s.end()
        counts = [c.count("\r\n") + 1 for c in s.stream.chunks]
        self.assertEqual(counts, [5, 5, 7, 7],
                         "每一帧都会补齐到历史最长, 这样才不留残影")

    def test_cursor_is_hidden_while_drawing(self):
        from bbdown_kit import console
        s = console.Screen(stream=_Collector())
        s.enabled = True
        seen = []
        s._move_home = lambda: seen.append("home")
        s._cursor = lambda visible: seen.append(("cursor", visible))
        s._write = lambda text: seen.append("write") or True
        s.begin()
        s.line("x")
        s.end()
        self.assertEqual(seen[0], ("cursor", False), "画之前要先藏光标")
        self.assertIn(("cursor", True), seen, "画完要把光标放回去")
        self.assertLess(seen.index(("cursor", False)), seen.index("write"),
                        "必须先藏光标再写")

    def test_falls_back_to_print_when_the_write_fails(self):
        """写失败(比如句柄其实是管道)必须立刻退回 print.

        这条是实测出来的: 输出被重定向时 GetStdHandle 返回的是**管道句柄**,
        WriteConsoleW 对它一律失败(返回 0、写出 0 个字符)。如果还硬走这条路,
        屏幕上就是什么都不显示 —— 比闪烁更糟。
        """
        from bbdown_kit import console
        s = console.Screen(stream=_Collector())
        s.enabled = True
        s._move_home = lambda: None
        s._cursor = lambda visible: None
        s._write = lambda text: False          # 假装写不出去
        s.begin()
        s.line("必须看得见这一行")
        s.end()
        self.assertFalse(s.enabled, "写失败之后要关掉这条捷径")
        self.assertTrue(any("必须看得见这一行" in c for c in s.stream.chunks),
                        "退回 print 之后内容必须真的出来")


class TestNoScreenClear(TempRootTest):
    """菜单不该再用 cls —— 那是闪烁的来源."""

    def _manager_source(self):
        from support import REAL_TOOLS
        with open(os.path.join(REAL_TOOLS, "BBDown-manager.py"),
                  encoding="utf-8") as f:
            return f.read()

    def test_manager_does_not_call_cls_in_code(self):
        """只看**可执行代码**里的字符串, 不看注释.

        (注释里特意写了"以前是 os.system(cls)", 用整段文本搜会误报;
        用 AST 只查真正会被执行的字符串常量。)
        """
        import ast
        tree = ast.parse(self._manager_source())
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    docstrings.add(doc)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if node.value in docstrings:
                continue
            self.assertNotIn("cls", node.value.lower(),
                             "第 %s 行还在用 cls" % getattr(node, "lineno", "?"))
            self.assertNotIn("os.system", node.value,
                             "第 %s 行还有 os.system" % getattr(node, "lineno", "?"))

    def test_menu_uses_the_screen(self):
        src = self._manager_source()
        self.assertIn("from bbdown_kit.console import Screen", src)
        self.assertIn("screen.begin()", src)
        self.assertIn("screen.end()", src)


class TestMenuLinesContent(TempRootTest):
    """画面内容(纯函数): 光标、选中、冻结标记、上下剩余."""

    def setUp(self):
        super().setUp()
        self.folders = [os.path.join("UP主下载", "%d_号%d" % (i, i))
                        for i in range(1, 6)]
        for name in self.folders:
            write_state(folder(*name.split(os.sep)),
                        videos=[{"aid": "1", "bvid": "", "title": "t"}])

    def _lines(self, cursor=0, selected=(), frozen=(), notice=""):
        import importlib.util
        from support import REAL_TOOLS
        spec = importlib.util.spec_from_file_location(
            "mgr_menu", os.path.join(REAL_TOOLS, "BBDown-manager.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        lines, _span = module.menu_lines(list(self.folders), cursor,
                                        set(selected), set(frozen), notice)
        return lines

    def test_cursor_marker_follows_the_cursor(self):
        for cursor in range(5):
            lines = self._lines(cursor=cursor)
            marked = [ln for ln in lines if ln.startswith(" > ")]
            self.assertEqual(len(marked), 1, "有且只有一行是光标行")
            self.assertIn("%d. " % (cursor + 1), marked[0])

    def test_selected_marker(self):
        lines = self._lines(cursor=3, selected={1, 3})
        # 光标行 + 已选中的组合
        self.assertTrue(any(ln.startswith(" > [x]") and "4." in ln
                            for ln in lines),
                        "光标所在的那行既有 > 也有 x")
        # 没被光标指着但被选中的行也要有 x
        picked = [ln for ln in lines if ln.startswith("   [x]")]
        self.assertEqual(len(picked), 1, "被选中但非光标行应该正好一行")
        self.assertIn("2.", picked[0])
        # 没选中的行不该有 x
        self.assertEqual(len([ln for ln in lines if "   [ ]" in ln]), 3)

    def test_frozen_tag(self):
        lines = self._lines(cursor=2, frozen={self.folders[2]})
        hit = [ln for ln in lines if "[已冻结" in ln]
        self.assertEqual(len(hit), 1)
        self.assertIn("3.", hit[0])
        self.assertIn(">", hit[0], "冻结的那行正好是光标行")

    def test_notice_line(self):
        lines = self._lines(notice="刚才干了什么")
        self.assertEqual(lines[-1], " 刚才干了什么")

    def test_header_and_summary_are_still_there(self):
        """这些字是用户认路的坐标, 重构渲染时不能丢."""
        lines = self._lines()
        blob = "\n".join(lines)
        for needle in ("BBDown 下载管理器 - 选择要更新的UP主",
                       "[↑/↓] 移动", "[空格] 选择/取消", "[回车] 开始更新",
                       "已选择 0 个UP主"):
            self.assertIn(needle, blob, "画面里少了 %r" % needle)

    def test_long_list_shows_remaining_counts(self):
        import importlib.util
        from support import REAL_TOOLS
        spec = importlib.util.spec_from_file_location(
            "mgr_menu2", os.path.join(REAL_TOOLS, "BBDown-manager.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        many = [os.path.join("UP主下载", "%d_x" % i) for i in range(300)]
        lines, (start, end) = module.menu_lines(many, 150, set(), set())
        self.assertIn("↑ 上面还有", "\n".join(lines))
        self.assertIn("↓ 下面还有", "\n".join(lines))
        self.assertTrue(start <= 150 < end, "光标必须在可见范围内")

    def test_span_is_returned_for_paging(self):
        import importlib.util
        from support import REAL_TOOLS
        spec = importlib.util.spec_from_file_location(
            "mgr_menu3", os.path.join(REAL_TOOLS, "BBDown-manager.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        many = [os.path.join("UP主下载", "%d_x" % i) for i in range(300)]
        _lines, (start, end) = module.menu_lines(many, 10, set(), set())
        self.assertGreaterEqual(end - start, 1, "翻页要用这个跨度")


class _Collector(object):
    def __init__(self):
        self.chunks = []

    def write(self, text):
        self.chunks.append(text)

    def flush(self):
        pass


class TestConsoleDetection(unittest.TestCase):
    """必须真的确认"写出去有人看得见", 不能只看句柄拿得到没有."""

    def test_available_means_a_real_console(self):
        """available() 为真时, 必须真的能往控制台写出字符.

        这条是实测出来的: 输出被重定向时 GetStdHandle 返回的是**管道句柄**,
        WriteConsoleW 对它一律失败(返回 0、写出 0 个字符)。所以判据不能是
        "句柄非空", 而必须是"这确实是个控制台"。
        """
        import ctypes
        from bbdown_kit import console
        if not console.available():
            self.skipTest("这台机器/这个环境下没有可用的控制台")
        h = console._handle()
        k = console._win()
        text = "x"
        written = ctypes.c_uint32(0)
        ok = k.WriteConsoleW(ctypes.c_void_p(h), text, len(text),
                             ctypes.byref(written), None)
        self.assertTrue(ok and written.value == len(text),
                        "available() 说能用, 但实际写不出去 —— 那就是误判")

    def test_api_calls_check_their_return_values(self):
        """_write 必须检查 WriteConsoleW 的返回值."""
        import ast
        from support import REAL_TOOLS
        with open(os.path.join(REAL_TOOLS, "bbdown_kit", "console.py"),
                  encoding="utf-8") as f:
            tree = ast.parse(f.read())
        src = ast.unparse(tree) if hasattr(ast, "unparse") else ""
        if src:
            self.assertIn("written.value", src,
                          "要看实际写出了多少字符, 不能写了就算成功")
            self.assertIn("GetConsoleMode", src,
                          "要用 GetConsoleMode 确认它真的是控制台")
            self.assertIn("CONOUT$", src,
                          "stdout 不是控制台时要退一步打开 CONOUT$")

    def test_falls_back_when_there_is_no_console_at_all(self):
        """彻底没有控制台时必须退回 print, 而不是什么都不显示."""
        from unittest import mock
        from bbdown_kit import console
        with mock.patch.object(console, "_handle", lambda: None):
            self.assertFalse(console.available())


if __name__ == "__main__":
    unittest.main()
