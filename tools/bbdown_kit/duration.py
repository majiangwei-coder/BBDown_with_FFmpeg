# -*- coding: utf-8 -*-
"""时长统计 —— 「这一轮要下的片子总共多长」, 给 order=duration_small/duration_large 用.

为什么用时长而不是"体积": 待下载的视频还没下, 本地没有它的文件, 体积只能瞎估
(拿名单里已下视频的平均体积乘个数 —— 而"一个都没下过"的名单正好是最需要排序的
那些, 它们只会拿到一个固定的兜底值, 乘出来等于按个数排, 白折腾)。时长不一样:
**列表接口本来就会把时长带回来**, 只是以前被丢掉了(投稿列表给 "length": "00:07",
合集给 "duration": 199), 现在存进 下载状态.json 的条目里, 不额外发一次请求。

所以这个模块**不扫磁盘、不发接口**: 就是读一遍状态文件(本库 352 份共 27.8 MB,
全部解析 0.2 秒), 把待下载视频的时长加起来。

一个名单的估算:

    待下载里已知时长的  -> 直接相加(精确)
    还有没时长的        -> 按"这份名单一个视频平均多长"补(见 average_of, 三级退:
                          待下载里已知的平均 -> 整份名单已知的平均 -> 全库中位数)
    整份名单都没有      -> 条数 × 全库中位数; 全库都没有(刚装上就是这种) ->
                          条数 × 1 秒, 也就是退化成"条数少的先跑", 不会乱排

**注意时长的来源是"这份名单上一次完整拉取"**: 老状态文件里一个 duration 都没有,
要等它下一次完整校验(默认 full_days=7, 或 --full / 菜单里的『F 全部完整校验』,
或 `--all --limit 0` 只刷列表不下载)才会补齐。在那之前这份名单按上面最后一行
退化成按条数排 —— 排序只会越来越准, 不会因为缺数据排乱。日志里会写清
"其中 N 个名单还没有时长数据", 不用猜。

这里的数字**只用来排队**: 不写进任何状态文件, 也不影响"某个视频要不要下"。
"""

from . import state as state_mod

_BLANK = {"count": 0, "seconds": 0, "known": 0, "list_seconds": 0,
          "list_known": 0}

# 文件夹 -> state.pending_summary() 的结果; 一次运行里只读一遍
_CACHE = {}
# 全库中位数(秒/个): measure() 之后固定下来, 免得"边排边算"导致排在前面的
# 名单和排在后面的名单用的不是同一个兜底值
_TYPICAL = [0]


def reset():
    """清掉缓存(测试用, 也可以用来强制重新算一遍)."""
    _CACHE.clear()
    _TYPICAL[0] = 0


# ---------------- 统计 ----------------

def measure(folders, log_fn=None):
    """读一遍这些名单的状态文件, 把时长统计收上来. 返回读了几份.

    为什么必须"先一次读完、再开始排序": 兜底值(全库中位数)得在**所有**排序
    key 算出来之前定下来。边排边算的话, 先被问到的名单和后被问到的名单用的
    不是同一个中位数, 结果就依赖调用顺序了。
    """
    todo = []
    for folder in folders:
        if folder not in _CACHE:
            _CACHE[folder] = None          # 占位, 免得同一批里重复读
            todo.append(folder)
    for folder in todo:
        try:
            _CACHE[folder] = state_mod.pending_summary(folder)
        except Exception:
            # 读不动就当"这份名单没有时长数据"(退回全库中位数):
            # 排序不该因为某一份名单读不了就整轮作废。
            _CACHE[folder] = dict(_BLANK)
    _TYPICAL[0] = _median()
    return len(todo)


def _median():
    """全库中位数(秒/个).

    先把每份名单各自算成"平均一个多长", 再取这些平均值的中位数 ——
    不把全库所有视频的时长堆在一起取中位数: 那样会被视频最多的几个大户
    (比如一个 11000 条的号)主导, 而这里要回答的是"一般的名单, 一个视频多长"。
    """
    averages = []
    for got in _CACHE.values():
        if not got:                 # 还没读到(占位)或读不出来: 跳过
            continue
        known = got["list_known"]
        if known > 0 and got["list_seconds"] > 0:
            averages.append(got["list_seconds"] / float(known))
    if not averages:
        return 0
    averages.sort()
    mid = len(averages) // 2
    if len(averages) % 2:
        return int(averages[mid])
    return int((averages[mid - 1] + averages[mid]) / 2.0)


def average_of(folder):
    """这份名单里一个视频平均多长(秒); 整份名单一条时长都没有 -> None.

    用**整份名单**(含已下载的)算: 待下载的那部分单拎出来常常样本太少
    (一条都没下过的名单一个样本都没有), 而"这个 UP主 的视频一般多长"
    本来就跟下没下过没关系。
    """
    got = _CACHE.get(folder)
    if not got or got["list_known"] <= 0:
        return None
    return got["list_seconds"] / float(got["list_known"])


def typical_seconds():
    """没有任何名单能测出平均时的兜底: 全库中位数(秒/个)."""
    if _TYPICAL[0]:
        return _TYPICAL[0]
    return _median()


def has_data(folder):
    """这份名单有没有时长数据(一条都没有 = 还没做过完整校验)."""
    got = _CACHE.get(folder)
    return bool(got and got["list_known"] > 0)


# ---------------- 估算 ----------------

def pending_seconds(folder):
    """这份名单待下载的视频总共多长(秒, 估算) —— 排序就按它."""
    got = _CACHE.get(folder)
    if got is None:
        try:
            got = state_mod.pending_summary(folder)
        except Exception:
            got = dict(_BLANK)
    count, seconds, known = got["count"], got["seconds"], got["known"]
    if count <= 0:
        return 0
    if known >= count:
        return seconds                 # 全知道: 这是精确值, 不是估算
    if known:
        average = seconds / float(known)        # 待下载里已知的那部分
    else:
        average = average_of(folder) or typical_seconds()   # 整份名单 -> 全库
    # 全库一个时长都没有(刚装上、还没做过完整校验)时用 1 秒/条:
    # 估算值等于待下载条数, 顺序自然变成"条数少的先跑" —— 总比所有名单都是 0、
    # 排出来跟没排一样强。
    return int(seconds + (count - known) * (average or 1))


def stats(folders):
    """日志用: (合计估算秒, 还有得下的名单数, 一条时长数据都没有的名单数)."""
    total = work = blank = 0
    for folder in folders:
        got = _CACHE.get(folder)
        if got is None:
            try:
                got = state_mod.pending_summary(folder)
            except Exception:
                got = dict(_BLANK)
        if got["count"] <= 0:
            continue
        work += 1
        total += pending_seconds(folder)
        if got["list_known"] <= 0:
            blank += 1
    return total, work, blank


def describe(seconds):
    """给人看的时长文字(0 要说成 "0 秒")."""
    if seconds <= 0:
        return "0 秒"
    from .util import duration_text
    return duration_text(seconds)


__all__ = ["reset", "measure", "average_of", "typical_seconds", "has_data",
           "pending_seconds", "stats", "describe"]
