# -*- coding: utf-8 -*-
"""控制台画面: 原地重写, 不闪.

为什么需要它:
    "上下选择 UP主"那个菜单以前每按一个键就 `os.system("cls")` 再全屏重画。
    `cls` 会先把整个屏幕**擦白**, 再一行行打上去 —— 中间那张白脸就是你看到的
    闪烁。屏幕越大、列表越长, 闪得越明显。

这里改成"原地重写":
    · 不擦屏 —— 直接把画面写到左上角, 盖掉旧的
    · **一次性**写出整帧(而不是一行一个 print), 中间没有半成品状态
    · 写之前把光标藏起来, 写完再放回去 —— 否则光标会在画面上乱跳
    · 新画面比旧画面短时, 用空格把多出来的行**盖掉**(不留残影)

用的是 Windows 控制台 API(ctypes), 不需要装任何东西(curses 在 Windows 上
本来也没有)。拿不到控制台(比如输出被重定向到文件、或者被守护当子进程拉起)
就自动退回普通 print —— 那种情况下没人看着, 闪不闪无所谓, 但不能因此报错。

**行为完全不变**: 画什么内容由调用方(菜单)决定, 这里只负责"把它稳稳地画上去"。
"""

import ctypes
import sys

# ---- Windows 控制台常量 ----
STD_OUTPUT_HANDLE = -11
FILE_TYPE_CHAR = 0x0002

_kernel32 = None
_api_ok = None


def _win():
    """拿到 kernel32; 不是 Windows 或拿不到就返回 None."""
    global _kernel32, _api_ok
    if _api_ok is False:
        return None
    if _kernel32 is None:
        if not sys.platform.startswith("win"):
            _api_ok = False
            return None
        try:
            _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            # 先声明好参数类型, 免得 64 位下句柄被截断
            _kernel32.GetStdHandle.argtypes = [ctypes.c_uint32]
            _kernel32.GetStdHandle.restype = ctypes.c_void_p
            _kernel32.GetFileType.argtypes = [ctypes.c_void_p]
            _kernel32.GetFileType.restype = ctypes.c_uint32
            _kernel32.GetConsoleMode.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            _kernel32.GetConsoleMode.restype = ctypes.c_int
            _kernel32.SetConsoleCursorPosition.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p]
            _kernel32.SetConsoleCursorPosition.restype = ctypes.c_int
            _kernel32.SetConsoleCursorInfo.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p]
            _kernel32.SetConsoleCursorInfo.restype = ctypes.c_int
            _kernel32.WriteConsoleW.argtypes = [
                ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
            _kernel32.WriteConsoleW.restype = ctypes.c_int
        except Exception:
            _kernel32 = None
            _api_ok = False
            return None
    return _kernel32


def _handle():
    """拿一个**真的控制台**句柄(拿不到返回 None).

    优先用标准输出句柄; 它不是控制台(被重定向成管道/文件)时, 再试着打开
    `CONOUT$` —— 那是"当前控制台"这个设备本身, 和 stdout 重定向无关。
    这样即使程序是被别的东西拉起来的、stdout 接了管道, 交互菜单照样能画出来;
    前面那个"退回 print"只在**真的没有控制台**时才会发生。
    """
    k = _win()
    if k is None:
        return None
    h = None
    try:
        h = k.GetStdHandle(STD_OUTPUT_HANDLE)
    except Exception:
        h = None
    if h and h != ctypes.c_void_p(-1).value and _is_console(h):
        return h
    return _open_conout()


def _is_console(handle):
    """这个句柄是不是控制台(字符设备 + 有控制台模式)."""
    k = _win()
    if k is None or not handle:
        return False
    try:
        if k.GetFileType(ctypes.c_void_p(handle)) != FILE_TYPE_CHAR:
            return False
        mode = ctypes.c_uint32(0)
        return bool(k.GetConsoleMode(ctypes.c_void_p(handle),
                                     ctypes.byref(mode)))
    except Exception:
        return False


def _open_conout():
    """打开 CONOUT$ (= 当前控制台). 拿不到返回 None."""
    global _conout_failed
    if _conout_failed:
        return None
    k = _win()
    if k is None:
        return None
    try:
        k.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32,
                                  ctypes.c_uint32, ctypes.c_void_p,
                                  ctypes.c_uint32, ctypes.c_uint32,
                                  ctypes.c_void_p]
        k.CreateFileW.restype = ctypes.c_void_p
        GENERIC_WRITE = 0x40000000
        GENERIC_READ = 0x80000000
        OPEN_EXISTING = 3
        handle = k.CreateFileW("CONOUT$", GENERIC_WRITE | GENERIC_READ,
                               2, None, OPEN_EXISTING, 0, None)
    except Exception:
        _conout_failed = True
        return None
    if not handle or handle == ctypes.c_void_p(-1).value:
        _conout_failed = True
        return None
    if not _is_console(handle):
        _conout_failed = True
        return None
    return handle


_conout_failed = False


def available():
    """能不能用控制台 API 画(而不是退回 print).

    **必须真的确认它是一个控制台**, 不能只看"句柄拿得到没有":
    实测输出被重定向(管道/文件)时, GetStdHandle 照样返回一个**管道句柄**,
    而 WriteConsoleW 对管道一律失败(返回 0、写出 0 个字符) —— 那时候如果
    还硬走这条路, 屏幕上就是**什么都不显示**, 比闪烁更糟。

    所以判据是"真的能拿到一个控制台句柄": 标准输出是控制台就用它,
    否则退一步开 CONOUT$; 两条路都不行才返回 False(那时才退回 print)。
    """
    return _handle() is not None


class Screen(object):
    """一屏画面. 只管"稳稳地画上去", 内容由调用方逐行给.

    用法:
        s = Screen()
        s.begin()            # 准备这一帧(藏光标)
        s.line("标题")
        s.line("")
        s.line(" > 选项一")
        s.end()              # 落笔(一次性写出, 多余的行擦掉, 放回光标)

    begin/end 之间不能有别的输出, 否则会插进画面里。
    """

    def __init__(self, stream=None):
        self.stream = stream if stream is not None else sys.stdout
        self.lines = []
        self.prev_count = 0        # 上一次画了多少行(用来擦掉多出来的)
        self.enabled = available()

    # ---- 组帧 ----

    def begin(self):
        self.lines = []
        if self.enabled:
            self._cursor(False)

    def line(self, text=""):
        self.lines.append(text)

    def lines_from(self, seq):
        for text in seq:
            self.lines.append(text)

    # ---- 落笔 ----

    def end(self):
        self.flush()

    def flush(self):
        """把这一帧画上去. 不擦屏, 原地盖掉旧的."""
        body = list(self.lines)
        # 上一帧比这一帧长: 用空行盖掉多出来的, 免得留残影
        # (不擦屏就必须自己做这件事, 否则删掉的选项还挂在屏幕上)
        extra = max(0, self.prev_count - len(body))
        body.extend([""] * extra)
        text = "\r\n".join(" " + t for t in body)
        self.prev_count = len(body)

        if self.enabled:
            # 万一写到一半失败(比如输出被重定向), 立刻改走 print ——
            # 屏幕上宁可"滚上去"也绝不能"什么都不显示"。
            if self._draw(text):
                try:
                    self.stream.flush()
                except Exception:
                    pass
                return
            self.enabled = False
        try:
            self.stream.write(text + "\n")
            self.stream.flush()
        except Exception:
            pass

    def _draw(self, text):
        """原地重写这一帧. 返回"确实画出去了"."""
        self._move_home()
        self._cursor(False)
        ok = self._write(text)
        self._cursor(True)
        return ok

    # ---- Windows 控制台 ----

    def _move_home(self):
        """把光标挪到左上角(0,0), 等价于 COORD{0,0}."""
        h = _handle()
        k = _win()
        if not h or k is None:
            return
        try:
            coord = ctypes.c_uint32(0)        # 低 16 位 X=0, 高 16 位 Y=0
            k.SetConsoleCursorPosition(ctypes.c_void_p(h), ctypes.byref(coord))
        except Exception:
            pass

    def _cursor(self, visible):
        """藏/放光标. 不藏的话重画时光标会在画面上乱跳."""
        h = _handle()
        k = _win()
        if not h or k is None:
            return
        try:
            # CONSOLE_CURSOR_INFO { DWORD dwSize; BOOL bVisible; }
            info = (ctypes.c_uint32 * 2)(25, 1 if visible else 0)
            k.SetConsoleCursorInfo(ctypes.c_void_p(h), ctypes.byref(info))
        except Exception:
            pass

    def _write(self, text):
        """一次性把整帧写出去(不是一行一个 write, 那样会看到逐行刷).

        返回 True = 确实写出去了。**必须检查返回值**: 对管道/文件句柄
        WriteConsoleW 一律失败, 不检查的话屏幕上会什么都不显示。
        """
        h = _handle()
        k = _win()
        if not h or k is None:
            return False
        try:
            written = ctypes.c_uint32(0)
            ok = k.WriteConsoleW(ctypes.c_void_p(h), text, len(text),
                                 ctypes.byref(written), None)
            return bool(ok) and written.value == len(text)
        except Exception:
            return False


def restore_cursor():
    """退出前把光标放回去(不然控制台里光标就没了, 看起来像卡死)."""
    h = _handle()
    k = _win()
    if not h or k is None:
        return
    try:
        info = (ctypes.c_uint32 * 2)(25, 1)
        k.SetConsoleCursorInfo(ctypes.c_void_p(h), ctypes.byref(info))
    except Exception:
        pass
