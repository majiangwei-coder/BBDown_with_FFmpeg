# -*- coding: utf-8 -*-
"""下载残留的 <aid> 临时工作目录: 认出来, 清掉.

BBDown 每下一个视频, 都会在名单文件夹里先建一个**以 aid 命名的纯数字目录**
(<UP主文件夹>\\<aid>\\), 分片(.vclip/.aclip)、封面(<aid>.jpg)、字幕
(<aid>.<subid>.zh-Hans.srt)、合并出来的 <aid>.P1.<cid>.mp4 全都先落在这里,
下完了再把成片搬到 <标题>_<aid>.mp4 (多P是 <标题>_<aid>\\[P01]...),
并把这个目录整个删掉。

所以数字目录只剩一个来源: **下载被掐断**(超时、限流、接管、Ctrl+C)。
而 BBDown 没有断点续传(--help 里没有任何 resume 选项; 实测重下同一个视频时
旧分片一个都不剩, 全被它自己先删掉), 这些半成品**不可能被复用** —— 留着
只是白占磁盘。实测一次就攒到 634 个目录 / 90.47 GB。

清理规则保守到不可能误伤成片, 四条全满足才动:

1. 目录名是纯数字(aid), 至少 6 位 —— 成片目录叫 <标题>_<aid>, 带下划线
2. 目录里没有子目录 —— 多P成片是多一层 <标题>_<aid>\\[P01]xxx.mp4
3. 每个文件都"属于这个 aid"且扩展名在临时产物清单里 —— 见 _belongs
4. 整个目录至少 10 分钟没动过 —— 正在下载的目录一直在写, 不会命中

任何一条不满足就一个字都不动: 宁可留个残骸, 也不能删错。
"""

import os
import re
import shutil
import time

from . import media
from .logging import log
from .util import size_text

# 多久没动过才算"不会再动了". 实测单个视频的下载窗口最长 5.6 分钟,
# 还在下载的目录每秒都在写, 10 分钟是给慢盘/卡顿留的余量。
STALE_SECONDS = 600

# 目录名至少这么长才算 aid(真实 aid 都是 9 位以上; 太短的纯数字目录
# 更可能是人手工建的序号目录, 不碰)
MIN_AID_LEN = 6

# BBDown 工作目录里会出现的临时产物. 成片扩展名也在里面, 但真正把关的是
# 文件名规则(_belongs): 临时目录里的成片一定叫 <aid>.P1.<cid>.mp4,
# 而交付的成片叫 <标题>_<aid>.mp4 —— 名字里带标题, 过不了 _belongs。
TEMP_EXTS = frozenset((
    ".vclip", ".aclip", ".m4s", ".part", ".download", ".tmp",
    ".jpg", ".jpeg", ".png", ".webp",
    ".srt", ".ass", ".xml",
    ".mp4", ".m4a", ".flv", ".mkv", ".mp3",
))

# 分片的老写法: 00000_<aid>.P4.<cid>.vclip (前导 5 位序号 + 下划线)
_INDEXED = re.compile(r"^(\d{5})_(.+)$")


class Found(object):
    """一个待清理的残留目录."""

    __slots__ = ("path", "name", "bytes")

    def __init__(self, path, name, num_bytes):
        self.path = path
        self.name = name
        self.bytes = num_bytes


def _belongs(base, aid):
    """去掉扩展名的文件名是不是这个 aid 的 BBDown 临时产物.

    实测出现过的三种: <aid>、<aid>.P1.<cid>、00000_<aid>.P4.<cid>。
    只要名字里带的是**别人的** aid(或者干脆不带 aid), 就说明这个目录不是
    纯粹的临时目录, 一律不碰。
    """
    if base == aid or base.startswith(aid + "."):
        return True
    m = _INDEXED.match(base)
    if m:
        rest = m.group(2)
        return rest == aid or rest.startswith(aid + ".")
    return False


def cleanable(path, name=None, now=None, stale_seconds=STALE_SECONDS):
    """这个目录能不能安全删掉. 返回 (能不能, 不能的原因).

    原因只用于日志/干跑输出 —— 看不懂为什么没清就去翻那一行。
    """
    name = name or os.path.basename(path)
    if not (name.isdigit() and len(name) >= MIN_AID_LEN):
        return False, "目录名不是纯数字 aid"
    try:
        entries = list(os.scandir(path))
    except OSError as e:
        return False, "读不到目录(%s)" % e
    newest = None
    for entry in entries:
        try:
            if entry.is_dir():
                return False, "里面有子目录, 不像临时目录"
        except OSError:
            return False, "读不到目录项"
        base, ext = os.path.splitext(entry.name)
        if ext.lower() not in TEMP_EXTS:
            return False, "有 %s 这种不像临时产物的文件" % entry.name
        if not _belongs(base, name):
            return False, "有不属于这个 aid 的文件: %s" % entry.name
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        newest = mtime if newest is None else max(newest, mtime)
    if newest is None:
        # 空目录(刚建好就被掐断): 只能看目录自己的时间
        try:
            newest = os.path.getmtime(path)
        except OSError:
            newest = now if now is not None else time.time()
    if stale_seconds > 0:
        # 注意这个判断: 必须用 > 而不是 >=, 而且要能容忍"文件比 now 还新"。
        # 时钟精度只有毫秒级, `now` 完全可能比刚写下的 mtime 早一点点 ——
        # 差值是 -0.001 秒这种事真的会发生。以前写成 `< stale_seconds`,
        # 于是 stale_seconds=0(调用方已经确认进程死透, 要求立刻清)时会被
        # 判成"刚刚还在变动", 该清的清不掉, 而且**时灵时不灵**。
        current = now if now is not None else time.time()
        if current - newest < stale_seconds:
            return False, "刚刚还在变动, 可能正在下载"
    return True, ""


def scan_folder(folder, now=None):
    """扫一个名单文件夹, 返回里面符合规则的残留目录列表."""
    found = []
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return found
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        if not (entry.name.isdigit() and len(entry.name) >= MIN_AID_LEN):
            continue
        ok, _why = cleanable(entry.path, entry.name, now=now)
        if ok:
            found.append(Found(entry.path, entry.name,
                               media.folder_bytes(entry.path)))
    return found


def scan_all(now=None):
    """扫全部名单文件夹(UP主下载 / 合集下载 / 单视频下载)."""
    from . import tasks          # 局部 import: 免得和 state/tasks 绕成环

    found = []
    for name in tasks.scan_all_folders():
        # 单视频下载的名单文件夹本身就是"装视频的目录", 数字目录直接躺在里面
        found.extend(scan_folder(os.path.join(_data_root(), name), now=now))
    return found


def _data_root():
    from . import paths
    return paths.data_root()


def _rmtree(path, tries=3):
    """删掉整个目录, 返回释放的字节数(删不掉就返回 0).

    刚 taskkill 完的进程在 Windows 上可能还攥着文件句柄, 所以重试几次;
    还是删不掉就放弃 —— 留给下一次扫描, 绝不硬来。
    """
    num_bytes = media.folder_bytes(path)
    for attempt in range(tries):
        try:
            shutil.rmtree(path)
            return num_bytes
        except OSError:
            if attempt == tries - 1:
                return 0
            time.sleep(0.5)
    return 0


def clean(found):
    """删掉扫描出来的这些目录. 返回 (删掉的个数, 释放的字节数)."""
    count = 0
    freed = 0
    for item in found:
        got = _rmtree(item.path)
        if got:
            count += 1
            freed += got
        elif not os.path.exists(item.path):
            count += 1          # 目录已经没了(空目录), 也算清掉
    return count, freed


def sweep(dry_run=False, now=None):
    """清一遍所有名单文件夹里的下载残留. 返回统计字典.

    每轮开跑前调一次, 兜住"被强杀/断电, 来不及自己清"的情况。
    """
    found = scan_all(now=now)
    if dry_run:
        return {"dirs": len(found), "bytes": sum(f.bytes for f in found),
                "cleaned": 0, "freed": 0, "items": found}
    count, freed = clean(found)
    return {"dirs": len(found), "bytes": sum(f.bytes for f in found),
            "cleaned": count, "freed": freed, "items": found}


def remove_after_kill(folder, aid):
    """掐断一个下载之后, 顺手清掉它留下的 <aid> 临时目录.

    这里的 stale 检查关掉(stale_seconds=0): 目录就是刚刚这一次下载建的,
    本来就不该等它满 10 分钟。除此之外的校验和每轮扫描**完全一样** ——
    拿不准就留着, 由每轮扫描或人工处理。
    返回释放的字节数。
    """
    path = os.path.join(folder, str(aid))
    if not os.path.exists(path):
        return 0
    ok, why = cleanable(path, str(aid), stale_seconds=0)
    if not ok:
        log("这次留下的临时目录没清(%s): %s" % (why, path))
        return 0
    freed = _rmtree(path)
    if freed:
        log("已清掉这次下载留下的临时目录 %s (%s)"
            % (os.path.basename(path), size_text(freed)))
    else:
        log("临时目录 %s 暂时删不掉(文件可能还被占着), 留给下一轮扫描"
            % os.path.basename(path))
    return freed
