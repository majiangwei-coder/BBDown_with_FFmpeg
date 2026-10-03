# -*- coding: utf-8 -*-
"""「这一轮先处理谁」的顺序设置.

    collection_first : 先合集/系列, 再 UP主(默认 —— 合集通常是特意去攒的)
    smallest         : 缺口小的先跑(小号一个个清掉, 名单快速变短)
    largest          : 缺口大的先跑
    name             : 按文件夹名, 合集排在 UP主 后面(老行为)

设置文件里写中文也认(合集优先 / 缺口小的优先 / ...)。
"""

DEFAULT_ORDER = "collection_first"

# 内部值 -> 给人看的说明(日志里直接用)
ORDER_TEXT = {
    "name": "按文件夹名",
    "collection_first": "合集优先",
    "smallest": "缺口小的优先",
    "largest": "缺口大的优先",
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
}


def normalize_order(value):
    """设置里写的顺序 -> 内部值. 认不出来就按默认(合集优先)."""
    return _ALIASES.get(str(value or "").strip().lower(), DEFAULT_ORDER)


def order_text(value):
    """内部值 -> 中文说明(日志用); 认不出来就原样返回."""
    return ORDER_TEXT.get(value, value)
