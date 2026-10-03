# -*- coding: utf-8 -*-
"""下载设置: 读 下载设置.txt.

设置文件是给人手改的, 所以:
  · 同一个设置项同时认英文名和中文名;
  · 编码按 UTF-8(带/不带BOM) -> GBK -> 宽松 的顺序试, 用记事本存过也不会崩;
  · 任何一行写坏了只跳过那一行, 不影响其它设置。

所有默认值集中在这里, 别处一律通过 load_settings() 取, 免得两个程序各写一套默认值。
"""

# 单视频下载上限(分钟): 防止一个卡死的 BBDown 把并发槽和整轮都拖住
DEFAULT_DOWNLOAD_TIMEOUT_MIN = 30
DEFAULT_FULL_DAYS = 7

DEFAULTS = {
    "parallel": 2,
    "interval": 1.0,
    "fail_threshold": 10,
    "pause_seconds": 300,
    "full_days": DEFAULT_FULL_DAYS,
    "download_timeout_minutes": DEFAULT_DOWNLOAD_TIMEOUT_MIN,
    "data_root": "",
    "order": "collection_first",
}

# 设置项 -> 认的名字(英文 + 中文), 第一个是规范名
_KEYS = {
    "parallel": ("parallel", "并行下载数"),
    "interval": ("interval", "下载间隔秒"),
    "fail_threshold": ("fail_threshold", "连续失败阈值"),
    "pause_seconds": ("pause_seconds", "失败暂停秒"),
    "full_days": ("full_days", "全量校验天数"),
    "download_timeout_minutes": ("download_timeout_minutes", "单视频超时分钟"),
    "data_root": ("data_root", "视频目录"),
    "order": ("order", "顺序"),
}
_ALIAS = {alias: canon for canon, aliases in _KEYS.items() for alias in aliases}


def _decode(raw):
    """设置文件的字节 -> 文本. 兼容 UTF-8 / GBK / UTF-16 / 别的.

    UTF-16 必须最先认: 记事本"另存为 Unicode"存出来的是 UTF-16LE, 而它的
    内容全是 ASCII 字符加 \x00, 对 utf-8 来说**是合法字节** —— 于是能"成功"
    解出一串带 NUL 的乱字符, 每一行都认不出来, 设置全部静默退回默认值。
    """
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16")
        except (UnicodeDecodeError, ValueError):
            pass
    for enc in ("utf-8-sig", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, ValueError):
            continue
    return raw.decode("utf-8", "replace")


def parse_settings_text(text):
    """设置文本 -> dict. 坏行跳过, 不抛异常."""
    from . import orders  # 局部导入: 避免 orders 反向依赖本模块

    out = dict(DEFAULTS)
    for line in text.splitlines():
        line = line.strip()
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        canon = _ALIAS.get(key.strip())
        if canon is None:
            continue
        value = value.strip()
        try:
            if canon == "parallel":
                out[canon] = max(1, int(value))
            elif canon == "interval":
                seconds = float(value)
                if seconds != seconds or seconds in (float("inf"),
                                                     float("-inf")):
                    raise ValueError("间隔秒必须是有限数")
                out[canon] = max(0.0, seconds)
            elif canon == "fail_threshold":
                out[canon] = max(1, int(value))
            elif canon == "pause_seconds":
                out[canon] = max(0, int(value))
            elif canon == "full_days":
                out[canon] = max(0, int(value))
            elif canon == "download_timeout_minutes":
                out[canon] = max(0, int(value))
            elif canon == "data_root":
                # 记事本里顺手写成 data_root="D:\videos" 很常见; 引号留着会
                # 变成路径里的非法字符, 后面建目录/写状态文件全都失败
                out[canon] = value.strip().strip('"').strip("'").strip()
            elif canon == "order":
                out[canon] = orders.normalize_order(value)
        except (ValueError, TypeError):
            continue
    return out


def load_settings(path=None):
    """读设置文件; 读不到/打不开就给默认值(不报错)."""
    if path is None:
        from . import paths
        path = paths.settings_file()
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return dict(DEFAULTS)
    return parse_settings_text(_decode(raw))
