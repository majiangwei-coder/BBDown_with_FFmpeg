# -*- coding: utf-8 -*-
"""小工具函数: 日志里给人看的文字, 文件名清洗, 编码兜底.

这里只放"没有任何项目知识"的纯函数, 方便随时单测.
"""

import datetime
import re

_NAME_BAD_CHARS = re.compile(r'[<>:"/\\|?*]')
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Windows 文件名里不允许的字符: 控制字符 + 保留字符。
# 除了 C0, 还收这些"看不见但能坏事"的: DEL、C1 控制区(U+0080-U+009F)、
# 行分隔符(U+2028/U+2029, 有些程序按换行处理, 能伪造日志行)、以及孤立代理
# 字符(U+D800-U+DFFF, JSON 里可以合法出现, 但写进文件/打印时会抛异常)。
_CONTROL = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff]")


def sanitize_name(name):
    """把视频/UP主标题变成合法的文件夹或文件名.

    只做 BBDown 需要的那点清洗(去掉 <>:"/\\|?* 和首尾的点), 其余原样保留,
    因为文件夹名要和 BBDown 实际写出的名字对得上 —— 改多了反而认不出本地文件。
    """
    name = _NAME_BAD_CHARS.sub("_", clean_text(name))
    return name.strip().strip(".") or "UP主"


def now_str():
    """标准时间戳: 2026-09-29 23:02:29."""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clock_str():
    """日志行首的短时间: 23:02:29."""
    return datetime.datetime.now().strftime("%H:%M:%S")


def elapsed_text(seconds):
    """123 -> "2分03秒", 45 -> "45秒". 日志里给人看的."""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "%d秒" % seconds
    return "%d分%02d秒" % (seconds // 60, seconds % 60)


def duration_text(seconds):
    """1830 -> "30分30秒", 7380 -> "2小时03分", 200000 -> "2天7小时".

    和 elapsed_text 的区别: 那个报的是"这一轮跑了多久"(最多到分, 够用);
    这个报的是"待下载的片子总时长", 动辄几百小时 —— 写成"12345分"没法看。
    """
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "%d秒" % seconds
    if seconds < 3600:
        return "%d分%02d秒" % (seconds // 60, seconds % 60)
    if seconds < 86400:
        return "%d小时%02d分" % (seconds // 3600, (seconds % 3600) // 60)
    return "%d天%d小时" % (seconds // 86400, (seconds % 86400) // 3600)


def size_text(num_bytes):
    """1234567 -> "1.2 MB"."""
    mb = num_bytes / 1048576.0
    if mb >= 1024:
        return "%.2f GB" % (mb / 1024.0)
    if mb >= 1:
        return "%.1f MB" % mb
    return "%.1f KB" % (num_bytes / 1024.0)


def strip_ansi(text):
    """去掉 BBDown 输出里的颜色转义, 否则日志里会夹着乱码."""
    return _ANSI.sub("", text)


def clean_text(value):
    """远端文本(标题/名字/接口消息) -> 控制字符已替换的文本.

    远端字符串里可能夹着控制字符: NUL 能在守护的事件管道里伪造哨兵, \n 和
    ESC 能在日志里伪造出新行、伪造颜色。BBDown 自己写文件名时就是把这类字符
    替换成下划线(实测 \t 和 | 都是), 所以这里也替换成下划线 —— 既堵住了日志
    伪造, 又保证清洗后的标题仍然能和磁盘上的文件名对得上。
    """
    if value is None:
        return ""
    return _CONTROL.sub("_", str(value))


def parse_stamp(text):
    """'2026-09-26 17:53:31' -> datetime; 认不出来返回 None."""
    try:
        return datetime.datetime.strptime(str(text or ""), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def to_int(value, default=None):
    """能转 int 就转, 否则给默认值(接口偶尔会返回字符串或 null)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
