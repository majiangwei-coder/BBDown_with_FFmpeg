# -*- coding: utf-8 -*-
"""守护的"轮次之间"维护: 只是把状态文件拨回正轨, 不做任何下载.

守护为什么需要这些: BBDown 的失败分两种, 一种是"这个视频真的下不了"
(充电专属), 一种是"现在被限流了"。前者该永久跳过, 后者必须当没发生过 ——
否则一轮限流就会把几百个视频写进「跳过」, 而「跳过」是永久不再重试的,
等于把视频从名单里删了。

所以:
    prepare_round        每轮开跑前: 把「跳过」放回队列(永久失败的除外),
                         把"有记录但本地没文件"的记录撤掉(重新下)
    prescreen_charging   开跑前批量确认哪些是充电专属, 免得一个个等失败(很慢)
    undo_round_damage    这一轮被判定限流 -> 撤回本轮新写进「跳过」的条目
    record_round_failures 正常结束的一轮 -> 给失败计数, 够了就标记永久失败

「不改状态: 只在两轮之间改 下载状态.json, 运行期间绝不动它」这条约定就是靠
这些函数只在轮次边界被调用实现的。
"""

import os

from . import bilitools, paths, state as state_mod, tasks
from .logging import log
from .util import now_str, parse_stamp

# 在"没被限流"的轮次里失败这么多次 -> 认定是充电/会员/已删除这类永久失败
PERMANENT_FAILS = 4

# 预筛的每轮预算: 一个文件夹缺口很大时, 抽查这么多条
PRESCREEN_BUDGET = 80
# 只查缺口 >= 这么多的文件夹(小的交给正常下载流程)
PRESCREEN_MIN_CANDIDATES = 8


def _each_state():
    """遍历所有名单文件夹的 State(跳过读不出来的)."""
    for name in tasks.scan_all_folders():
        folder = os.path.join(paths.data_root(), name)
        if not os.path.exists(paths.state_file(folder)):
            continue
        yield name, folder, state_mod.State.load(folder)


def _is_permanent(quarantined, aid):
    """名录里的这一条是不是"永久失败"(只有它才配永远不再重试).

    名录本身可能是 dict(aid -> 记录) 或集合(老调用点); 条目又可能是手工
    改坏的数字/字符串 —— 两种怪形状都不该让整轮崩掉。
    """
    try:
        record = quarantined.get(aid)
    except AttributeError:
        return aid in quarantined
    return bool(record.get("permanent")) if isinstance(record, dict) else False


def _since_round(value, round_start):
    """这条记录是不是"本轮写进去的".

    必须按时间解析后比较: 直接拿字符串比大小的话, "守护: 永久失败" 这种
    中文标记在编码上大于任何数字, 会被当成"每轮都是新写的", 于是每个干净轮
    都给同一批视频的失败次数 +1, 攒到 PERMANENT_FAILS 就被误标成永久失败。
    认不出时间戳的标记(守护写的几种中文 note)一律当"不是本轮的"。
    """
    stamp = parse_stamp(value)
    start = parse_stamp(round_start)
    if stamp is None or start is None:
        return False
    return stamp >= start


def prepare_round(quarantined, do_ghost=True):
    """轮次开始前的维护.

    返回 {"re_queued": 放回队列数, "ghosts": 撤回的假记录数}。
    """
    re_queued = 0
    ghosts = 0
    kept = 0
    for name, folder, st in _each_state():
        titles = st.titles()
        changed = False

        # 「跳过」-> 队列(只有"永久失败"的才留着)
        #
        # 判据必须是名录里的 permanent 标志: 只是"这轮又失败了"的记录
        # (fails=1、2、3...)必须放回队列继续重试。以前按"在不在名录里"判断,
        # 于是失败一次的视频被当成永久失败, 从此再也不下载 —— 这是全项目
        # 唯一会悄悄丢视频的地方, 判据不能松。
        for aid in list(st.skip):
            if aid not in titles:
                del st.skip[aid]          # 视频已不在名单里(UP主删了), 顺手清掉
                changed = True
            elif _is_permanent(quarantined, aid):
                kept += 1
            else:
                del st.skip[aid]
                re_queued += 1
                changed = True

        # 永久失败名录里的 -> 确保它确实在「跳过」里(补写)
        for aid in list(quarantined):
            if (_is_permanent(quarantined, aid) and aid in titles
                    and aid not in st.skip and aid not in st.record):
                st.force_skip(aid, titles[aid], state_mod.TIME_PERMANENT)
                changed = True

        # "有记录但本地没文件" -> 撤回记录, 重新下载
        if do_ghost and st.record:
            index = _media_index(folder)
            dup = st.duplicate_titles()
            removed = st.drop_ghost_records(
                lambda aid, title: index.has(aid, title, dup))
            if removed:
                ghosts += len(removed)
                changed = True

        if changed:
            st.save()
    if re_queued or ghosts or kept:
        log("本轮准备: 放回跳过 %d 个, 重新下载(有记录没文件) %d 个, 保留永久失败 %d 个"
            % (re_queued, ghosts, kept))
    return {"re_queued": re_queued, "ghosts": ghosts}


def _media_index(folder):
    from . import media
    return media.MediaIndex(folder)


def prescreen_charging(quarantined, budget=PRESCREEN_BUDGET):
    """缺口很大的文件夹, 先批量查一遍哪些是充电/付费专属.

    这类视频必然下载失败, 一个个等失败太慢(一个大文件夹能耗掉几小时),
    所以每轮开跑前用 view 接口抽查一小批, 确认是充电的就直接永久跳过。
    只查候选 >= PRESCREEN_MIN_CANDIDATES 的文件夹, 小的交给正常下载流程。
    """
    checked = 0
    found = 0
    for name, folder, st in _each_state():
        if checked >= budget:
            break
        index = _media_index(folder)
        dup = st.duplicate_titles()
        candidates = [
            v for v in st.videos
            if v.get("aid") and v["aid"] not in st.record
            and v["aid"] not in st.skip and v["aid"] not in quarantined
            and not index.has(v["aid"], v.get("title", ""), dup)
        ]
        if len(candidates) < PRESCREEN_MIN_CANDIDATES:
            continue
        changed = False
        for video in candidates:
            if checked >= budget:
                break
            aid = video["aid"]
            checked += 1
            if not bilitools.video_unplayable(aid):
                continue
            quarantined[aid] = {
                "title": video.get("title", ""), "fails": 0, "permanent": True,
                "reason": "充电/付费专属", "folder": name, "last": now_str(),
            }
            st.force_skip(aid, video.get("title", ""), state_mod.TIME_CHARGING)
            found += 1
            changed = True
        if changed:
            st.save()
    if checked:
        state_mod.save_json_atomic(paths.quarantine_file(), quarantined)
        log("预筛: 查了 %d 个视频, 其中充电/付费专属 %d 个(已永久跳过)"
            % (checked, found))
    return checked


def undo_round_damage(round_start, quarantined):
    """被判定限流的一轮: 把这轮新写进「跳过」的撤回, 让它们下次重新下载.

    round_start 是这一轮开始时的时间戳字符串(YYYY-mm-dd HH:MM:SS),
    「跳过」条目里的 time 比它新就说明是这一轮写的。
    """
    reverted = 0
    for _name, _folder, st in _each_state():
        titles = st.titles()
        changed = False
        for aid, entry in list(st.skip.items()):
            if _is_permanent(quarantined, aid):
                continue
            if aid in titles and _since_round(entry.get("time"), round_start):
                del st.skip[aid]
                reverted += 1
                changed = True
        # 失败计数也要一起退。只撤「跳过」是漏的: 限流这一轮留下的 count=1
        # 会一直挂在「失败」里, 下一次真失败就凑够 2 次进「跳过」, 再攒到
        # PERMANENT_FAILS 就成了永久跳过 —— "限流当没发生过"必须连计数一起撤。
        for aid, entry in list(st.failed.items()):
            if not _since_round(entry.get("time"), round_start):
                continue
            count = int(entry.get("count", 0)) - 1
            if count > 0:
                entry["count"] = count
            else:
                del st.failed[aid]
            changed = True
        if changed:
            st.save()
    return reverted


def record_round_failures(round_start, quarantined):
    """正常结束的一轮: 给这轮失败的视频累加计数.

    返回够格永久跳过的 aid 列表(调用方负责标记 permanent 并写盘)。
    """
    newly = []
    for name, _folder, st in _each_state():
        for section in (st.skip, st.failed):
            for aid, entry in section.items():
                if not isinstance(entry, dict):
                    continue          # 手工改坏的条目, 跳过
                if not _since_round(entry.get("time"), round_start):
                    continue
                record = quarantined.get(aid)
                if not isinstance(record, dict):
                    record = {"title": entry.get("title", ""), "fails": 0}
                try:
                    record["fails"] = int(record.get("fails", 0)) + 1
                except (TypeError, ValueError):
                    record["fails"] = 1
                record["folder"] = name
                record["last"] = now_str()
                quarantined[aid] = record
                if record["fails"] >= PERMANENT_FAILS and not record.get("permanent"):
                    newly.append(aid)
    return newly


def prune_quarantine(quarantined, alive_aids, done_aids=frozenset()):
    """清掉永久失败名录里的过期条目. 返回被清掉的 aid 列表.

    两种算过期: 已经不在任何名单里(UP主删了), 或者**已经下载成功了** ——
    下都下到了还留着"它失败过", 只会让预筛永远跳过它, 名录还会无限长大。
    """
    stale = [aid for aid in quarantined
             if aid not in alive_aids or aid in done_aids]
    for aid in stale:
        quarantined.pop(aid, None)
    return stale


def alive_aids():
    """所有名单里出现过的 aid(判断永久失败名录有没有过期用)."""
    return _aids()[0]


def done_aids():
    """已经下载成功的 aid(名录里的这些条目可以清掉了)."""
    return _aids()[1]


def _aids():
    """(名单里出现过的 aid, 已下载成功的 aid) —— 一次遍历算两边."""
    alive = set()
    done = set()
    for _name, _folder, st in _each_state():
        for video in st.videos:
            if video.get("aid"):
                alive.add(video["aid"])
        for aid in st.record:
            done.add(aid)
    return alive, done


def forget_permanent(aids):
    """把指定 aid 从永久失败名录里删掉(用户手动重试时用). 返回删掉的列表."""
    aids = set(aids)
    if not aids:
        return []
    data = state_mod.load_json(paths.quarantine_file(), {})
    if not isinstance(data, dict):
        return []
    gone = [aid for aid in aids if aid in data]
    for aid in gone:
        data.pop(aid, None)
    if gone:
        state_mod.save_json_atomic(paths.quarantine_file(), data)
    return gone
