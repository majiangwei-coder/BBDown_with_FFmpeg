# -*- coding: utf-8 -*-
"""「这一轮先处理谁」的顺序设置.

    collection_first : 先合集/系列, 再 UP主(默认 —— 合集通常是特意去攒的)
    smallest         : 缺口小的先跑(数**个数**, 小号一个个清掉, 名单快速变短)
    largest          : 缺口大的先跑(同样数个数)
    duration_small   : 待下载**总时长**短的先跑(见 duration 模块)
    duration_large   : 待下载总时长长的先跑
    name             : 按文件夹名, 合集排在 UP主 后面(老行为)

smallest/largest 数的是**条数**, duration_* 量的是**时长** —— 两者常常排成完全
不同的顺序: 一个缺 11000 条、每条 7 秒的名单(合计 21 小时)远比一个缺 100 条、
每条 1 小时的名单(合计 100 小时)轻松。想"从短的下起"就用 duration_small,
它用的是列表接口本来就返回、以前被丢掉的那个时长(见 duration 模块)。

设置文件里写中文也认(合集优先 / 缺口小的优先 / 时长短的优先 / ...)。
"""

DEFAULT_ORDER = "collection_first"

# 按时长排的取值: 用它们时 order_tasks 会先把名单里的时长统计一遍(见 duration 模块)
DURATION_ORDERS = ("duration_small", "duration_large")

# 内部值 -> 给人看的说明(日志里直接用)
ORDER_TEXT = {
    "name": "按文件夹名",
    "collection_first": "合集优先",
    "smallest": "缺口小的优先",
    "largest": "缺口大的优先",
    "duration_small": "时长短的优先",
    "duration_large": "时长长的优先",
}

_ALIASES = {
    "name": "name", "默认": "name", "名字": "name", "按名字": "name",
    "collection_first": "collection_first", "collections": "collection_first",
    "合集优先": "collection_first", "合集": "collection_first",
    "先合集": "collection_first",
    "smallest": "smallest", "smallest_first": "smallest",
    "缺口小的优先": "smallest", "小的优先": "smallest", "小优先": "smallest",
    "largest": "largest", "largest_first": "largest",
    "缺口大的优先": "largest", "大的优先": "largest", "大优先": "largest",
    # 按时长(秒)排 —— 别名多给几个, 免得记不住该写哪个
    "duration_small": "duration_small", "duration": "duration_small",
    "time_small": "duration_small", "short_first": "duration_small",
    "时长短的优先": "duration_small", "时长小的优先": "duration_small",
    "短时长优先": "duration_small", "按时长": "duration_small",
    "按总时长": "duration_small", "时长": "duration_small",
    "时长从短到长": "duration_small",
    "duration_large": "duration_large", "time_large": "duration_large",
    "long_first": "duration_large",
    "时长长的优先": "duration_large", "时长大的优先": "duration_large",
    "长时长优先": "duration_large", "时长从长到短": "duration_large",
    # 兼容: 上一版叫"按体积排"(bytes_small/bytes_large), 但那套本质是
    # "本地平均体积 × 个数", 对没下过的名单等于按个数排, 已经换成按时长。
    # 老写法照旧认, 免得设置文件里还写着 bytes_small 的人一下子掉回默认顺序。
    "bytes_small": "duration_small", "bytes": "duration_small",
    "size_small": "duration_small", "small_bytes": "duration_small",
    "体积小的优先": "duration_small", "小的体积优先": "duration_small",
    "小体积优先": "duration_small", "按体积": "duration_small",
    "体积": "duration_small", "体积从小到大": "duration_small",
    "bytes_large": "duration_large", "size_large": "duration_large",
    "large_bytes": "duration_large",
    "体积大的优先": "duration_large", "大的体积优先": "duration_large",
    "大体积优先": "duration_large", "体积从大到小": "duration_large",
}


def normalize_order(value):
    """设置里写的顺序 -> 内部值. 认不出来就按默认(合集优先)."""
    return _ALIASES.get(str(value or "").strip().lower(), DEFAULT_ORDER)


def order_text(value):
    """内部值 -> 中文说明(日志用); 认不出来就原样返回."""
    return ORDER_TEXT.get(value, value)


def is_duration_order(value):
    """这个顺序是不是"按时长排"(要的话得先把名单里的时长统计一遍)."""
    return value in DURATION_ORDERS
