# -*- coding: utf-8 -*-
"""三份名单的盘点与处理顺序.

videos\\ 下面有三个平级的大类目录, 每一类都自带 下载状态.json:

    UP主下载\\<UID>_<名字>\\     一个 UP主一份名单(投稿接口)
    合集下载\\<名字>_<编号>\\     一个合集/系列一份名单(合集接口)
    单视频下载\\                 手工下的零散视频, 共一份名单

老布局(startswith 数字_ 的文件夹直接躺在 videos\\ 下)也照样认, 搬家搬一半
不会漏人。这里只负责"有哪些名单、这一轮先跑谁", 不负责下载。
"""

import os
import re

from . import freeze, paths, state as state_mod
from .logging import log
from .orders import ORDER_TEXT
from .util import sanitize_name

UP = paths.UP_NAME
COLL = paths.COLL_NAME
SINGLE = paths.SINGLE_NAME


def is_up_folder_name(name):
    """UP主文件夹的命名规则: <UID>_<名字>."""
    return bool(re.match(r"^\d+_", str(name or "")))


def scan_up_folders():
    """所有 UP主 文件夹, 返回相对数据目录的路径.

    新布局 UP主下载\\<UID>_<名字> -> "UP主下载/<UID>_<名字>"
    老布局 <UID>_<名字>(搬家之前) -> "<UID>_<名字>"
    """
    folders = []
    for base in (UP, ""):
        root = paths.in_data(base) if base else paths.data_root()
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            if not is_up_folder_name(name):
                continue
            if not os.path.isdir(os.path.join(root, name)):
                continue
            folders.append("%s%s%s" % (base, os.sep, name) if base else name)
    folders.sort()
    return folders


def scan_collection_folders():
    """所有合集/系列文件夹(必须带状态文件才算, 手工放进来的空目录不算)."""
    result = []
    root = paths.collection_dir()
    try:
        names = os.listdir(root)
    except OSError:
        return result
    for name in names:
        full = os.path.join(root, name)
        if os.path.isdir(full) and os.path.exists(paths.state_file(full)):
            result.append("%s%s%s" % (COLL, os.sep, name))
    result.sort()
    return result


def scan_single(folder=None, include_record=False):
    """单视频下载: 只有登记过名单才纳入盘点, 空文件夹不凑数.

    默认只看「投稿列表」(和历史行为一致)。include_record=True 时连
    「已下载」里那些"以前只记了记录、没登记名单"的老数据也算上, 一键更新/
    守护需要把它们一起补进名单, 所以走那条路时用 True。
    """
    folder = folder or paths.single_dir()
    if not os.path.isdir(folder):
        return 0
    data = state_mod.load_json(paths.state_file(folder), None)
    if not isinstance(data, dict):
        return 0
    videos = data.get(state_mod.S_VIDEOS) or []
    if not include_record:
        return len(videos)
    record = data.get(state_mod.S_RECORD) or {}
    return len(videos) or len(record)


def scan_all_folders(include_frozen=False, registry=None):
    """守护要盘点的所有名单文件夹(相对数据目录), 三类都算.

    顺序和各段内部的排序规则与历史行为保持一致(UP主在前、再合集、再单视频),
    因为状态报告里的行序是按这个顺序列出来的。

    冻结的名单默认**不在**里面 —— 这就是"冻结"生效的地方: 守护刷新名单、
    补缺口、清残留的时候看不到它, 于是不会去动它。要连冻结的一起看
    (比如 --status 报账、--audit 自检), 传 include_frozen=True。

    registry 可以传一个已经读好的冻结集合, 免得反复读文件。
    """
    from . import freeze

    if registry is None:
        registry = freeze.frozen_set()

    out = list(scan_up_folders())
    for name in scan_collection_folders():
        if name not in out:
            out.append(name)
    if scan_single():
        out.append(SINGLE)
    if include_frozen:
        return out
    return [name for name in out if name not in registry]


def group_of(relative_name):
    """相对路径 -> 属于哪一类(报告里分类汇总用)."""
    head = str(relative_name).split(os.sep)[0]
    if head in (UP, COLL, SINGLE):
        return head
    return "老布局UP主"


def folder_to_mid(folder):
    """从文件夹名里取出 UID; 允许传入 "UP主下载/123_某某" 这种相对路径."""
    name = os.path.basename(str(folder).replace("\\", "/"))
    m = re.match(r"^(\d+)_", name)
    return m.group(1) if m else None


def up_folder_for(mid, name):
    """新 UP主 的专属文件夹(必须落在 UP主下载\\ 下, 不能掉在 videos\\ 根)."""
    return os.path.join(paths.up_dir(), "%s_%s" % (mid, sanitize_name(name)))


def state_spec_from_folder(folder_rel):
    """从合集文件夹的状态文件里读回 类型/mid/编号, 用来刷新它."""
    meta = state_mod.read_meta(os.path.join(paths.data_root(), folder_rel))
    kind = meta.get(state_mod.M_KIND)
    if (kind not in ("合集", "系列") or not meta.get(state_mod.M_MID)
            or not meta.get(state_mod.M_COLL_ID)):
        return None
    return {
        "类型": kind,
        "mid": str(meta[state_mod.M_MID]),
        "id": str(meta[state_mod.M_COLL_ID]),
        "名称": meta.get(state_mod.M_NAME) or str(meta[state_mod.M_COLL_ID]),
    }


# ---------------- 任务 ----------------

def build_all_tasks():
    """一键更新/守护每轮要处理的全部任务.

    返回 (up任务, 合集任务, 单视频任务); 任务是个 (类型, 载荷) 二元组:
        ("existing", "UP主下载/123_某某")     已有 UP主
        ("collection", {"spec": ..., "folder": ...})
        ("single", {"folder": ..., "count": n})
    """
    up_tasks = [("existing", folder) for folder in scan_up_folders()
                if folder_to_mid(folder)]
    coll_tasks = []
    for rel in scan_collection_folders():
        spec = state_spec_from_folder(rel)
        if spec is None:
            log("跳过 %s: 状态文件里没有合集信息(重新粘贴一次合集链接即可)" % rel)
            continue
        coll_tasks.append(("collection", {
            "spec": spec,
            "folder": os.path.join(paths.data_root(), rel),
        }))
    single_tasks = []
    count = 0
    try:
        # 顺手把"只有已下载记录、没有名单"的老数据补进名单
        state_mod.ensure_single_list(paths.single_dir())
        count = scan_single(include_record=True)
    except Exception as e:
        log("读取单视频清单失败(跳过它): %s" % e)
    if count:
        single_tasks.append(("single", {"folder": paths.single_dir(),
                                        "count": count}))

    # 冻结的名单从这里剔除 —— 一键更新和定时任务走的都是这条路, 所以
    # 这就是"冻结之后再也不更新、不下载"的实现点。
    frozen = freeze.frozen_set()
    if frozen:
        before = len(up_tasks) + len(coll_tasks) + len(single_tasks)
        up_tasks = [t for t in up_tasks if t[1] not in frozen]
        coll_tasks = [t for t in coll_tasks
                      if os.path.relpath(t[1]["folder"],
                                         paths.data_root()) not in frozen]
        single_tasks = [t for t in single_tasks
                        if SINGLE not in frozen]
        skipped = before - (len(up_tasks) + len(coll_tasks) + len(single_tasks))
        if skipped:
            log("有 %d 个名单已冻结, 本次不处理(想启用就用 --unfreeze)"
                % skipped)
    return up_tasks, coll_tasks, single_tasks


def task_folder(task):
    """任务 -> 它对应的文件夹(相对数据目录的用 os.path.join 拼成绝对路径)."""
    kind, payload = task
    if kind in ("collection", "single"):
        return payload["folder"]
    return os.path.join(paths.data_root(), payload)


def pending_of(task):
    """这个任务大概还差多少个(只读状态文件, 不扫目录) —— 只用于排序."""
    return state_mod.pending_count(task_folder(task))


def order_tasks(up_tasks, coll_tasks, single_tasks, order):
    """按 order 把三类任务排成这一轮的处理顺序."""
    if order == "name":
        return list(up_tasks) + list(coll_tasks) + list(single_tasks)
    if order == "collection_first":
        return list(coll_tasks) + list(up_tasks) + list(single_tasks)
    rest = list(up_tasks) + list(coll_tasks)
    rest.sort(key=pending_of, reverse=(order == "largest"))
    # 单视频那点零头放最后, 免得一直插队
    return rest + list(single_tasks)


def order_preview(tasks, n=4):
    """给日志用: 这一轮开头要处理的几个是谁."""
    bits = [os.path.basename(task_folder(t)) for t in tasks[:n]]
    text = " → ".join(bits)
    if len(tasks) > n:
        text += " → ... 共 %d 个" % len(tasks)
    return text


def order_label(order):
    return ORDER_TEXT.get(order, order)


def collection_spec_from_state(folder_rel):
    """兼容旧名字(守护和管理器都用过)."""
    return state_spec_from_folder(folder_rel)


def scan_collection_folders_specs():
    """合集任务用的 (spec, folder) 列表(诊断输出用)."""
    out = []
    for rel in scan_collection_folders():
        spec = state_spec_from_folder(rel)
        if spec:
            out.append((spec, os.path.join(paths.data_root(), rel)))
    return out


__all__ = [
    "UP", "COLL", "SINGLE",
    "is_up_folder_name", "scan_up_folders", "scan_collection_folders",
    "scan_single", "scan_all_folders", "group_of",
    "folder_to_mid",
    "up_folder_for", "state_spec_from_folder", "collection_spec_from_state",
    "scan_collection_folders_specs", "build_all_tasks", "task_folder",
    "pending_of", "order_tasks", "order_preview", "order_label",
]
