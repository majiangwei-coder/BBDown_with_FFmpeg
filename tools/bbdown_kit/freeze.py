# -*- coding: utf-8 -*-
"""冻结名单: 暂停某些 UP主 / 合集 的"更新与下载", 但把它的名单原样留着.

为什么单独存一个文件, 而不是塞进 下载状态.json:
    那个文件是"每个名单的唯一真相", 也是历史包袱最重的地方(155 个文件、中文键名、
    增量同步基线)。冻结是"我暂时不想管它", 不是名单本身的状态 —— 塞进去要改契约、
    要迁移、还要考虑程序读到不认识的键怎么办。单开一个文件, 契约一个字都不用动,
    而且冻掉/启用只是往文件里加一行或删一行, 不可能碰到名单本身。

存在哪: <项目根>\\logs\\冻结名单.json
    它跟 videos\\ 分开, 所以挪动/改名 videos 目录不会丢冻结记录。
    反过来, 冻结是按**文件夹名**认的 —— 你要是手动改了 UP主文件夹的名字,
    这条冻结就匹配不上了(程序会在 --audit 里提示), 重新冻结一次即可。

冻掉之后会发生什么(这就是"冻结"的定义):
    · 守护每轮的名单里**没有它** -> 不刷新投稿列表、不下载
    · 下载管理器的一键更新 / 定时任务里**没有它** -> 同上
    · 缺口统计里**不计入它** -> "待补齐"只反映真正要干的事
    · 它的 下载状态.json / 已下载 / 跳过 一个字都不动 -> 解冻后从原地继续
    · 已经在下的会在下一轮停下(它不在名单里了); 想立刻停就用 安全停止
"""

import os

from . import paths
from . import state as state_mod
from .logging import log
from .util import now_str

# 条目里的键名(中文, 和 下载状态.json 的风格保持一致)
F_FOLDER = "文件夹"
F_TIME = "冻结时间"
F_REASON = "原因"


def _load():
    """读冻结名单. 读不到 / 读坏了都当成"什么都没冻结"(不能因此拦住下载)."""
    data = state_mod.load_json(paths.frozen_file(), None)
    if not isinstance(data, dict):
        return {}
    out = {}
    for key, value in data.items():
        if isinstance(value, dict):
            out[str(key)] = value
        elif value in (True, None, ""):
            out[str(key)] = {}      # 手工写成 {"文件夹": true} 也认
    return out


def _save(data):
    try:
        os.makedirs(os.path.dirname(paths.frozen_file()), exist_ok=True)
    except OSError:
        pass
    return state_mod.save_json_atomic(paths.frozen_file(), data)


def load():
    """{文件夹: 条目}."""
    return _load()


def frozen_set():
    """只要键集合, 判断用."""
    return set(_load().keys())


def is_frozen(folder_rel, registry=None):
    """这个名单文件夹冻上了吗.

    registry 传进来就用它(一次读盘、多次判断), 免得在一轮里反复读文件。
    """
    if registry is None:
        registry = _load()
    return str(folder_rel) in registry


def freeze(folder_rel, reason="", when=None):
    """冻掉一个名单文件夹. 返回 (是否成功, 说明)."""
    folder_rel = str(folder_rel or "").strip()
    if not folder_rel:
        return False, "没有指定要冻结的名单文件夹"
    # 先确认它真的存在, 免得冻结一个拼错的名字还以为成功了
    full = os.path.join(paths.data_root(), folder_rel)
    if not os.path.exists(paths.state_file(full)):
        return False, ("找不到这个名单(里面没有 %s): %s"
                       % (paths.STATE_NAME, folder_rel))
    data = _load()
    if folder_rel in data:
        return False, "它已经在冻结名单里了: %s" % folder_rel
    data[folder_rel] = {
        F_FOLDER: folder_rel,
        F_TIME: when or now_str(),
        F_REASON: reason or "",
    }
    if not _save(data):
        return False, "写入冻结名单失败(logs 目录可能不可写)"
    detail = ("(%s)" % reason) if reason else ""
    log("已冻结: %s %s —— 不再更新、不再下载, 名单原样保留"
        % (folder_rel, detail))
    return True, "已冻结"


def unfreeze(folder_rel):
    """启用一个被冻结的名单文件夹. 返回 (是否成功, 说明)."""
    folder_rel = str(folder_rel or "").strip()
    data = _load()
    if folder_rel not in data:
        return False, "它不在冻结名单里: %s" % folder_rel
    entry = data.pop(folder_rel)
    if not _save(data):
        return False, "写入冻结名单失败(logs 目录可能不可写)"
    log("已启用: %s (冻结于 %s) —— 下一轮就会重新检查它的更新"
        % (folder_rel, (entry or {}).get(F_TIME, "?")))
    return True, "已启用"


def entries():
    """冻结条目列表: [{"文件夹":..., "冻结时间":..., "原因":...}, ...]."""
    data = _load()
    rows = []
    for key in sorted(data):
        row = dict(data[key] or {})
        row.setdefault(F_FOLDER, key)
        rows.append(row)
    return rows


def names():
    """只返回文件夹名(已排序)."""
    return [row[F_FOLDER] for row in entries()]


def match_one(text):
    """把用户给的东西变成一个名单文件夹名. 认不出来返回 None.

    接受三种写法, 都是日常真的会顺手敲的:
        "UP主下载\\123456789_某某UP主"   完整相对路径
        "123456789_某某UP主"             文件夹名(自动补上 UP主下载\\)
        "123456789"                          UID
    """
    from . import tasks

    text = str(text or "").strip().strip('"').strip("'")
    if not text:
        return None
    text = text.replace("/", os.sep)

    # 1) 完整相对路径(或已经带 \ 的写法)
    if os.sep in text:
        if os.path.exists(paths.state_file(os.path.join(paths.data_root(), text))):
            return text
        # 也允许只写到子目录名
        base = os.path.basename(text)
        text = base

    # 2) 和磁盘上的名单逐个比: 先比完整名, 再比"去掉 UP主下载\"的短名
    candidates = (tasks.scan_up_folders() + tasks.scan_collection_folders()
                  + [tasks.SINGLE])
    for name in candidates:
        if text == name or text == os.path.basename(name):
            return name

    # 3) 当 UID 认
    if text.isdigit():
        for name in tasks.scan_up_folders():
            if tasks.folder_to_mid(name) == text:
                return name
    return None


def stale_entries():
    """冻结名单里"文件夹已经不存在"的条目(改名/删掉了).

    留着不害事(只是永远匹配不上), 但值得在 --audit 里点一下。
    """
    from . import tasks

    existing = set(tasks.scan_up_folders())
    existing.update(tasks.scan_collection_folders())
    existing.add(tasks.SINGLE)
    return [name for name in names() if name not in existing]


def cleanup_stale():
    """删掉失效的冻结条目. 返回删掉的名字列表."""
    stale = stale_entries()
    if not stale:
        return []
    data = _load()
    for name in stale:
        data.pop(name, None)
    _save(data)
    return stale


def all_report_lines():
    """给 --freeze-list 用的一页清单."""
    rows = entries()
    if not rows:
        return ["当前没有被冻结的名单。", "",
                "冻结: 下载守护.bat --freeze 123456789        (按 UID)",
                "     下载守护.bat --freeze \"UP主下载\\123_某某\"  (按文件夹)",
                "启用: 下载守护.bat --unfreeze 123456789"]
    lines = ["当前被冻结的名单: %d 个（不计入待补齐，也不会被更新）" % len(rows), ""]
    lines.append("%-46s %-20s %s" % ("名单文件夹", "冻结时间", "原因"))
    for row in rows:
        lines.append("%-46s %-20s %s"
                     % (str(row.get(F_FOLDER, ""))[:46],
                        str(row.get(F_TIME, ""))[:20],
                        row.get(F_REASON, "") or ""))
    lines.append("")
    lines.append("启用: 下载守护.bat --unfreeze <UID 或 文件夹名>")
    stale = stale_entries()
    if stale:
        lines.append("")
        lines.append("注意: 有 %d 条已经匹配不上任何文件夹(改名/删了?): %s"
                     % (len(stale), ", ".join(stale[:5])))
        lines.append("      清掉它们: 下载守护.bat --freeze-clean")
    return lines
