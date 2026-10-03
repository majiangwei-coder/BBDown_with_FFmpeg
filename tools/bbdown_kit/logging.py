# -*- coding: utf-8 -*-
"""控制台/文件输出: 统一日志、机器可读事件、颜色、管道解码.

这里解决三个以前散在各处的问题:

1. 编码。管理器(BBDown)按系统 ANSI 代码页写日志, 自己却是 UTF-8,
   同一个管道里两种编码混着来, 所以读管道时一行一行试 UTF-8 -> GB18030。
2. 事件。守护以前靠正则去猜中文日志的措辞(改了文案就统计错), 现在管理器
   额外往 .jsonl 里写一行一条的结构化事件, 措辞怎么改都不影响统计;
   老版本管理器不写事件时仍可退回解析文本(见 runner)。
3. 颜色。Windows 控制台默认不认 ANSI, 要主动打开, 否则日志里全是转义码。
"""

import json
import os
import re
import sys
import threading

# 这几个是"对外沿用"的名字: 别的模块(含两个入口)历史上从这里取它们
from .util import clock_str, now_str, strip_ansi  # noqa: F401

# 事件流里每行前面的哨兵: 守护从合并的管道里一眼认出"这行是事件不是日志"
EVENT_PREFIX = "\x00EVENT\x00"

# 当前程序在日志行前面的标签(守护/管理器各自设一下)
TAG = ""

# 事件文件(--event-log 指定); None = 不写事件(老用法零影响)
EVENTS_FILE = None
_EVENT_LOCK = threading.Lock()

COLOR_CODES = {
    "cyan": "96",
    "green": "92",
    "yellow": "93",
    "red": "91",
    "magenta": "95",
    "blue": "94",
}


def setup_console():
    """输出统一成 UTF-8, 并让 Windows 控制台认 ANSI 颜色.

    重定向到文件时也是 UTF-8, 不会再和别的日志编码打架。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def highlight(text, color="cyan"):
    """把文字加粗并着色, 便于在控制台里一眼看到."""
    return "\033[1;%sm%s\033[0m" % (COLOR_CODES.get(color, "96"), text)


# 日志行里绝不能出现的字符(会"变出"新的一行出来): 换行/回车/NUL/行分隔符。
# ESC 不在其中 —— 高亮(highlight)故意用它上色, 控制台认它。
_LOG_FORGE = re.compile(r"[\r\n\x00\u2028\u2029]")


def log(msg, to_file=None):
    """打一行 [时:分:秒] 日志; 给了 to_file 就同时追加进那个文件.

    出口统一把换行类字符换成空格: 远端标题里万一夹着换行, 日志文件里就会
    凭空多出一行, 看起来像程序自己写的。
    """
    msg = _LOG_FORGE.sub(" ", str(msg))
    line = "[%s] %s%s" % (clock_str(), ("%s: " % TAG) if TAG else "", msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    if to_file:
        try:
            with open(to_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
    return line


# ---------------- 机器可读事件 ----------------
#
# 加 --event-log <文件> 时, 每发生一件事就往文件里追加一行 JSON, 例如:
#   {"t":"2026-09-25 18:11:11","kind":"video_end","aid":"123","ok":true,"rc":0}
# 守护读这个文件, 就不用再去猜中文日志的措辞了。

# 管理器会发出的事件(守护认这些):
#   run_start / run_end          一次运行开始/结束
#   up_start / up_end / up_pending   一个名单文件夹开始/结束/待下载数
#   video_start / video_end      单个视频开始/结束(ok/rc/new_file/timeout/elapsed)
#   video_skipped                进"跳过"列表
#   login_failed / lock_busy     登录失效 / 已有实例在跑


def set_event_file(path):
    """设置事件输出文件(None = 关闭)."""
    global EVENTS_FILE
    EVENTS_FILE = path
    return EVENTS_FILE


def peek_event_file(argv):
    """拿锁/联网之前先把 --event-log 读出来, 这样连"另一个实例在跑"也能记上."""
    path = None
    for i, arg in enumerate(argv):
        if arg == "--event-log" and i + 1 < len(argv):
            path = argv[i + 1]
        elif arg.startswith("--event-log="):
            path = arg.split("=", 1)[1]
    if path:
        set_event_file(path)
    return path


def emit(kind, **fields):
    """写一行事件. 没开 --event-log 时什么都不做."""
    if not EVENTS_FILE:
        return
    rec = {"t": now_str(), "kind": kind}
    rec.update(fields)
    try:
        line = json.dumps(rec, ensure_ascii=False)
    except (TypeError, ValueError):
        return
    try:
        with _EVENT_LOCK:
            with open(EVENTS_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        pass


def decode_line(raw):
    """管道里的一行字节 -> 字符串: 先当 UTF-8, 不行再当 GB18030.

    BBDown(.NET)按系统 ANSI(中文系统 = GBK)往管道里写, 我们的 python 是
    UTF-8, 所以必须逐行两种都试, 否则总有一边是乱码。
    """
    if isinstance(raw, str):
        return raw
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")
