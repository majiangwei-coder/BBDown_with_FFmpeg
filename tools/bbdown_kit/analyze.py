# -*- coding: utf-8 -*-
"""缺口统计与自检报告(只读).

守护的 --status 和 --audit 都在这里。为什么要把"报数"单独拆出来:
以前两份报告用两套算法各自数一遍, 出现了 status_report 掉了一段(实际被
后面重名函数覆盖)这种"看日志和看状态文件对不上"的问题。现在只有一个算法,
报告只是它的不同呈现。

术语(和日志里用的一致):
    名单      投稿列表里的视频总数
    已记录    已下载 里的条数
    缺文件(ghost)  有已下载记录, 但本地找不到对应文件
    没下过        名单里有, 但既没记录也没跳过
    待补齐    ghost + (没下过 - 永久失败的)
"""

import os

from . import media, paths, state as state_mod, tasks
from .util import now_str


class FolderReport(object):
    """一个名单文件夹的账."""

    __slots__ = ("folder", "total", "recorded", "ghost", "missing", "pending",
                 "skipped", "failed", "quarantined", "list_complete",
                 "api_count", "frozen")

    def __init__(self, folder, total, recorded, ghost, missing, pending,
                 skipped, failed, quarantined, list_complete, api_count,
                 frozen=False):
        self.folder = folder
        self.total = total
        self.recorded = recorded
        self.ghost = ghost                    # aid 列表
        self.missing = missing                # aid 列表
        self.pending = pending                # aid 列表(不含永久失败)
        self.skipped = skipped
        self.failed = failed
        self.quarantined = quarantined
        self.list_complete = list_complete
        self.api_count = api_count
        self.frozen = frozen                  # 被冻结: 不计入待补齐

    @property
    def gap(self):
        """缺口 = 缺文件的 + 还没下的."""
        return len(self.ghost) + len(self.pending)

    @property
    def downloaded(self):
        return self.total - len(self.missing) - len(self.ghost)

    def incomplete(self):
        return self.list_complete is False


def _is_permanent(quarantined, aid):
    """名录里这一条是不是"永久失败".

    名录可能是 dict(aid -> 记录), 也可能是老调用点传进来的集合; 条目本身
    还可能是手工改坏的字符串/数字 —— 三种都不能让它崩。
    """
    try:
        record = quarantined.get(aid)
    except AttributeError:
        return aid in quarantined
    return bool(record.get("permanent")) if isinstance(record, dict) else False


def folder_report(folder_rel, quarantined=frozenset(), frozen=False):
    """统计一个名单文件夹. 没有状态文件返回 None.

    frozen=True 表示这个名单被冻结了: 数字照算(你还能看到它缺多少),
    但**不计入"待补齐"** —— 冻结的意思就是"暂时不补它"。
    """
    folder = os.path.join(paths.data_root(), folder_rel)
    st = state_mod.State.load(folder, readonly=True)
    if not os.path.exists(paths.state_file(folder)):
        return None
    index = media.MediaIndex(folder)
    dup = st.duplicate_titles()

    ghost, missing = [], []
    for video in st.videos:
        aid = video.get("aid")
        if not aid:
            continue
        exists = index.has(aid, video.get("title", ""), dup)
        if aid in st.record:
            if not exists:
                ghost.append(aid)
        elif not exists:
            missing.append(aid)
    pending = [aid for aid in missing
               if not _is_permanent(quarantined, aid)]
    return FolderReport(
        folder=folder_rel,
        total=len(st.videos),
        recorded=len(st.record),
        ghost=ghost,
        missing=missing,
        pending=pending,
        skipped=len(st.skip),
        failed=len(st.failed),
        quarantined=sum(1 for aid in missing
                        if _is_permanent(quarantined, aid)),
        list_complete=st.is_list_complete(),
        api_count=st.meta.get(state_mod.M_TOTAL),
        frozen=frozen,
    )


def all_reports(quarantined=frozenset()):
    """全部名单的账. 冻结的也在里面, 只是带 frozen 标记.

    为什么冻结的还要算进来: 你需要看得见"它还在、只是停着"。
    但它的缺口不该混进"待补齐", 否则那个数字永远变不干净 ——
    这件事由 summarize / status_lines 里按 frozen 过滤完成。
    """
    from . import freeze

    registry = freeze.frozen_set()
    reports = []
    for name in tasks.scan_all_folders(include_frozen=True, registry=registry):
        report = folder_report(name, quarantined, frozen=name in registry)
        if report is not None:
            reports.append(report)
    return reports


def summarize(reports, exclude_frozen=True):
    """所有文件夹加起来的账.

    exclude_frozen=True 时, 冻结的名单不计入任何"还差多少"的数字 ——
    冻结的定义就是"暂时不补它"。但 folders 一律算全部, 好让报告能说
    "共 N 个, 其中冻结 M 个"。
    """
    counted = [r for r in reports
               if not (exclude_frozen and getattr(r, "frozen", False))]
    return {
        "folders": len(reports),
        "active": len(counted),
        "frozen": len(reports) - len(counted),
        "total": sum(r.total for r in counted),
        "recorded": sum(r.recorded for r in counted),
        "ghost": sum(len(r.ghost) for r in counted),
        "missing": sum(len(r.missing) for r in counted),
        "pending": sum(len(r.pending) for r in counted),
        "skipped": sum(r.skipped for r in counted),
        "failed": sum(r.failed for r in counted),
        "quarantined": sum(r.quarantined for r in counted),
    }


def total_gap(quarantined=frozenset()):
    """当前还差多少个(守护每轮开始前用它决定要不要继续). 冻结的不算."""
    totals = summarize(all_reports(quarantined))
    return totals, totals["ghost"] + totals["pending"]


def top_pending(quarantined=frozenset(), n=5):
    """缺口最大的几个文件夹, 用来在日志里一行说清"这轮主要在补什么"."""
    rows = [(r.folder, r.gap) for r in all_reports(quarantined)
            if r.gap and not getattr(r, "frozen", False)]
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows[:n]


# ---------------- --status ----------------

def status_lines(quarantined=frozenset(), only_problem=True):
    """给人看的进度报告. 返回 (合计, 文本行).

    注意"已下载"一律用 totals["recorded"](已下载那一节的条数),
    不要写成 total - missing - ghost: 那样会把 ghost 算两遍, 还会把
    "跳过"的视频算成已下载(以前就是这么算的, 数字一直偏小)。
    """
    from .procs import running_now_text

    reports = all_reports(quarantined)
    totals = summarize(reports)
    lines = [
        "当前运行中: %s" % running_now_text(),
        "统计时间: %s" % now_str(),
        "名单文件夹: %d 个(%s\\ + %s\\ + %s)%s"
        % (totals["folders"], tasks.UP, tasks.COLL, tasks.SINGLE,
           ("，其中已冻结 %d 个（不计入下面的数字）" % totals["frozen"])
           if totals["frozen"] else ""),
        "名单视频共 %d 个, 已下载 %d 个"
        % (totals["total"], totals["recorded"]),
        "缺文件(有记录但本地找不到): %d 个" % totals["ghost"],
        "没下过: %d 个 (其中被判定永久失败的 %d 个)"
        % (totals["missing"], totals["quarantined"]),
        "待补齐总数: %d 个" % (totals["ghost"] + totals["pending"]),
        "",
        "%-8s %-46s %8s %8s %8s %8s"
        % ("缺口", "文件夹", "名单", "已记录", "缺文件", "没下过"),
    ]
    bad = [r for r in reports
           if (r.ghost or r.pending or r.missing)
           and not getattr(r, "frozen", False)]
    bad.sort(key=lambda r: r.gap, reverse=True)
    for r in bad:
        lines.append("%-8d %-46s %8d %8d %8d %8d"
                     % (r.gap, r.folder, r.total, r.recorded,
                        len(r.ghost), len(r.missing)))

    # 冻结的单独列一块: 你能看见"它还在、只是停着", 而且它的缺口不会混进上面
    frozen = [r for r in reports if getattr(r, "frozen", False)]
    if frozen:
        lines.append("")
        lines.append("已冻结 %d 个(不更新、不下载; 缺口不计入待补齐):"
                     % len(frozen))
        for r in sorted(frozen, key=lambda r: r.gap, reverse=True):
            lines.append("   %-44s 名单 %5d, 已下载 %5d, 冻结时缺口 %d"
                         % (r.folder, r.total, r.recorded, r.gap))
        lines.append("   启用: 下载守护.py --unfreeze <UID 或 文件夹名>")

    if not only_problem or not bad:
        lines.append("")
        lines.append("没有问题: 没有缺文件的记录, 也没有没下过的视频")
    return totals, lines


def status_report(quarantined=frozenset(), only_problem=True):
    """兼容旧调用点: 返回 (合计, 文本行)."""
    return status_lines(quarantined, only_problem)


# ---------------- --audit ----------------

def audit_lines(quarantined=frozenset()):
    """深度自检(只读): 三类目录各自的账 + 有没有漏在管理之外的东西.

    --status 只报"还缺多少", 这个把"会不会漏"也一起查了:
    哪份名单不完整(可能被截断)、哪个文件夹名字不合法(程序认不出来)、
    永久失败名录里有没有已经过期的条目, 等等。
    """
    reports = all_reports(quarantined)
    totals = summarize(reports)
    by_group = {}
    for r in reports:
        g = by_group.setdefault(tasks.group_of(r.folder), {
            "folders": 0, "total": 0, "recorded": 0,
            "ghost": 0, "missing": 0, "skip": 0, "fail": 0, "incomplete": 0})
        g["folders"] += 1
        g["total"] += r.total
        g["recorded"] += r.recorded
        g["ghost"] += len(r.ghost)
        g["missing"] += len(r.missing)
        g["skip"] += r.skipped
        g["fail"] += r.failed
        if r.incomplete():
            g["incomplete"] += 1

    lines = ["自检时间: %s" % now_str(), ""]
    lines.append("%-14s %6s %8s %8s %8s %8s %8s" % (
        "类别", "文件夹", "名单", "已记录", "缺文件", "没下过", "跳过"))
    for name in (tasks.UP, tasks.COLL, tasks.SINGLE, "老布局UP主"):
        g = by_group.get(name)
        if not g:
            continue
        lines.append("%-14s %6d %8d %8d %8d %8d %8d" % (
            name, g["folders"], g["total"], g["recorded"],
            g["ghost"], g["missing"], g["skip"]))
    lines.append("")
    lines.append("合计: 名单 %d 条, 已记录 %d 条, 缺文件 %d 条, 没下过 %d 条"
                 " (其中永久失败 %d 条)"
                 % (totals["total"], totals["recorded"],
                    totals["ghost"], totals["missing"], totals["quarantined"]))
    if totals["frozen"]:
        lines.append("另有 %d 个名单已冻结(上面的合计**不含**它们):"
                     % totals["frozen"])
        for r in reports:
            if getattr(r, "frozen", False):
                lines.append("   %s (名单 %d 条, 已下载 %d)" % (r.folder, r.total,
                                                            r.recorded))

    problems = _problems(reports, quarantined)
    lines.append("")
    if problems:
        lines.append("需要留意 %d 条:" % len(problems))
        lines.extend("  - " + p for p in problems)
    else:
        lines.append("需要留意: 没有")
    return lines


def _problems(reports, quarantined):
    """把所有"会漏东西"的情况找出来."""
    problems = []

    # 1) 名单不完整(接口截断/翻页上限/接口异常) —— 最容易被忽略的"漏"
    for r in reports:
        if r.incomplete():
            if r.api_count is None:
                # 单视频下载这种没有接口基线的名单: 不完整是正常的, 说清楚就好
                problems.append(
                    "没有接口基线(每次都按事实重新盘点, 不影响下载): %s 名单 %d 条"
                    % (r.folder, r.total))
            else:
                problems.append(
                    "名单不完整(会重新全量拉): %s 名单 %d 条, 接口报告 %s 条"
                    % (r.folder, r.total, r.api_count))

    # 2) UP主下载 下名字不合法 -> 程序认不出 UID, 永远同步不到
    up_root = paths.up_dir()
    try:
        for name in sorted(os.listdir(up_root)):
            if (os.path.isdir(os.path.join(up_root, name))
                    and not tasks.is_up_folder_name(name)):
                problems.append("名字不合规(程序认不出来): %s%s%s"
                                % (tasks.UP, os.sep, name))
    except OSError:
        pass

    # 3) 视频目录里"没有状态文件"的目录(可能是手动放的, 不会进名单)
    for base in (tasks.UP, tasks.COLL):
        root = paths.in_data(base)
        try:
            for name in sorted(os.listdir(root)):
                full = os.path.join(root, name)
                if (os.path.isdir(full)
                        and not os.path.exists(paths.state_file(full))):
                    problems.append("没有 %s, 不会被盘点: %s%s%s"
                                    % (paths.STATE_NAME, base, os.sep, name))
        except OSError:
            pass

    # 4) 永久失败名录里已经不在任何名单里的条目(可清理)
    alive = set()
    for r in reports:
        st = state_mod.State.load(os.path.join(paths.data_root(), r.folder),
                                  readonly=True)
        for video in st.videos:
            if video.get("aid"):
                alive.add(video["aid"])
    stale = sorted(aid for aid in quarantined if aid not in alive)
    if stale:
        problems.append("永久失败名录里有 %d 条已经不在任何名单里(可以删掉): %s"
                        % (len(stale), ", ".join(stale[:5])))

    # 5) 冻结名单里匹配不上任何文件夹的条目(文件夹被改名/删了)
    from . import freeze

    stale_frozen = freeze.stale_entries()
    if stale_frozen:
        problems.append(
            "冻结名单里有 %d 条匹配不上任何文件夹(改名或删了?) —— "
            "重新冻结一次, 或执行 --freeze-clean 清掉: %s"
            % (len(stale_frozen), ", ".join(stale_frozen[:5])))
    return problems


def audit_report(quarantined=frozenset()):
    return audit_lines(quarantined)


# ---------------- 单行摘要(日志里用) ----------------

def gap_line(quarantined=frozenset()):
    """一行说清现在的缺口构成, 例如:
    "缺口 12 个 (缺文件 3, 没下过 9, 其中永久失败 4)"
    """
    totals = summarize(all_reports(quarantined))
    return ("缺口 %d 个 (缺文件 %d, 没下过 %d, 其中永久失败 %d)"
            % (totals["ghost"] + totals["pending"], totals["ghost"],
               totals["pending"], totals["quarantined"]))


def top_line(quarantined=frozenset(), n=5):
    """缺口最大的几个文件夹, 拼成一行."""
    top = top_pending(quarantined, n)
    if not top:
        return ""
    return "; ".join("%s 缺 %d" % (folder, count) for folder, count in top)


def quick_gap(quarantined=frozenset()):
    """只要数字, 不要文本(守护主循环用)."""
    return total_gap(quarantined)
