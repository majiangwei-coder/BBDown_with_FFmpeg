# -*- coding: utf-8 -*-
"""下载状态.json —— 每个名单文件夹里唯一的真相文件.

这份文件是程序之间、程序与人工之间唯一的状态契约, 所以集中在这里读写,
别处一律通过 State 对象操作, 不再自己开文件解析。

文件长这样(键名是中文, 历史原因, 不能改, 改了旧数据就认不出来)::

    {
      "投稿列表": [ {"aid": "1173...", "bvid": "BV1...", "title": "标题"}, ... ],
      "已下载":   { "<aid>": {"title": ..., "bvid": ..., "time": ...}, ... },
      "跳过":     { "<aid>": {"title": ..., "time": "守护: 永久失败, 不再重试"}, ... },
      "失败":     { "<aid>": {"title": ..., "count": 2, "time": ...}, ... },
      "同步信息": { ... 增量同步的基线, 见下 ... }
    }

五节的含义(实测 155 个文件都符合这个结构):

  投稿列表  该名单当前**完整**的视频清单。aid 一律是字符串 —— 一旦变成 int 就和
            「已下载」的键对不上, 所有视频会被当成没下过而重下一遍。
  已下载    下成功的。这是"跳过下载"的唯一依据。
  跳过      不再重试的(充电/付费专属、连续失败 2 次)。**这是唯一会丢东西的地方**:
            误加一条 = 这个视频永远不再下载; 所以只有"连续失败 2 次"或
            "接口确认充电专属"才写它, 被限流打断的轮次会把这些条目撤回。
  失败      暂存的失败计数(count), 下次运行自动重试; count 到 2 就移进「跳过」。
  同步信息  增量同步的基线, 有两种形态:
              UP主 : 最新aid / 视频总数 / 列表完整 / 全量时间 / 同步方式
              合集 : 类型 / mid / 编号 / 名称 / 视频总数 / 列表完整 / 全量时间 / 同步方式
            靠它才能"只发 1 次请求"就判断出有没有新投稿; 丢了只会退化成完整拉取,
            不会出错, 所以它是纯加速信息。

「跳过」和「失败」里的 time 还兼作"这条是什么时候写的"用, 守护靠比较时间戳
判断哪些条目是"这一轮刚写进去的"(被限流时要撤回的就是这些)。
"""

import datetime
import json
import os
import re
import time

from . import paths
from .util import now_str, to_int

# JSON 里的键名(中文 —— 这是已经写进 155 个文件的数据契约, 绝对不能改)
S_VIDEOS = "投稿列表"
S_RECORD = "已下载"
S_SKIP = "跳过"
S_FAILED = "失败"
M_META = "同步信息"

# 「跳过」条目里 time 字段的几种特殊取值(都是本程序自己写的, 靠它区分原因)
TIME_PERMANENT = "守护: 永久失败, 不再重试"
TIME_CHARGING = "守护: 充电/付费专属"
TIME_RECOVERED = "已按本地文件补记"
TIME_LOCAL_EXISTS = "本地文件已存在"

# 同步信息的键
M_LAST_AID = "最新aid"
M_TOTAL = "视频总数"
M_COMPLETE = "列表完整"
M_FULL_TIME = "全量时间"
M_MODE = "同步方式"
M_KIND = "类型"          # 合集才有: 合集 / 系列
M_MID = "mid"
M_COLL_ID = "编号"
M_NAME = "名称"


class State(object):
    """一个名单文件夹的状态.

    典型的用法是"读一次, 改一会儿, 存一次":

        st = State.load(folder)
        st.videos = synced
        st.mark_done(aid, title, bvid)
        st.save()
    """

    __slots__ = ("folder", "videos", "record", "skip", "failed", "meta")

    def __init__(self, folder, videos=None, record=None, skip=None,
                 failed=None, meta=None):
        self.folder = folder
        self.videos = videos if videos is not None else []
        self.record = record if record is not None else {}
        self.skip = skip if skip is not None else {}
        self.failed = failed if failed is not None else {}
        self.meta = meta if meta is not None else {}

    # ---------- 读 ----------

    @classmethod
    def load(cls, folder, readonly=False):
        """读状态文件; 文件不存在/读坏了就从老格式迁移或从空开始.

        旧文件(已下载.json / 跳过.json / 投稿列表.txt)只有在本文件缺失时才会被读,
        读进来之后转成统一格式并把旧文件删掉 —— 迁移只发生一次。

        readonly=True 时一个字节都不写: 不清理 .tmp、不做老格式迁移 ——
        --status / --audit 这类"只读"命令走这条路, 免得和正在写状态文件的
        管理器互相干扰(以前它们会删掉对方正在写的 .tmp)。
        """
        if not readonly:
            cleanup_temp(folder)
        path = paths.state_file(folder)
        data, broken = _load_state_json(path)
        if data is not None and not isinstance(data, dict):
            # 合法 JSON、但不是对象(比如整份被改成了 [] 或 null): 和"读不出来"
            # 一样对待 —— 绝不能当成空状态, 否则下一次 save 就拿空的把它盖了
            _warn("状态文件内容不是对象(%s), 已按坏文件处理"
                  % type(data).__name__)
            data, broken = None, True
        if broken and not readonly:
            _quarantine_broken(path)
        if isinstance(data, dict):
            meta = data.get(M_META)
            return cls(
                folder,
                videos=_as_list(data.get(S_VIDEOS)),
                record=_as_dict(data.get(S_RECORD)),
                skip=_as_dict(data.get(S_SKIP)),
                failed=_as_dict(data.get(S_FAILED)),
                meta=meta if isinstance(meta, dict) else {},
            )
        if readonly:
            return cls(folder)
        state = cls._migrate_legacy(folder)
        if state is not None:
            return state
        return cls(folder)

    @classmethod
    def _migrate_legacy(cls, folder):
        """老格式(三个分开的文件) -> 统一状态文件. 没有老文件就返回 None."""
        legacy_record = os.path.join(folder, paths.LEGACY_RECORD_NAME)
        legacy_skip = os.path.join(folder, paths.LEGACY_SKIP_NAME)
        legacy_list = os.path.join(folder, paths.LEGACY_LIST_NAME)
        if not any(os.path.exists(p) for p in
                   (legacy_record, legacy_skip, legacy_list)):
            return None
        record = _read_json(legacy_record)
        skip = _read_json(legacy_skip)
        videos = []
        text = _read_text(legacy_list)
        if text:
            for line in text.splitlines():
                m = re.search(r"/av(\d+)", line)
                if m:
                    videos.append({"aid": m.group(1), "bvid": "",
                                   "title": "av%s" % m.group(1)})
        state = cls(folder, videos,
                    record if isinstance(record, dict) else {},
                    skip if isinstance(skip, dict) else {})
        if state.save():
            remove_legacy_files(folder)
        return state

    def save(self):
        """原子写: 先写 .tmp 再改名, 断电/崩溃不会留下半个文件."""
        path = paths.state_file(self.folder)
        payload = {
            S_VIDEOS: self.videos,
            S_RECORD: self.record,
            S_SKIP: self.skip,
            S_FAILED: self.failed,
            M_META: self.meta,
        }
        ok = _write_json_atomic(path, payload)
        if not ok:
            _warn("状态文件写不进去: %s (这一次的状态改动没落盘)" % path)
        return ok

    # ---------- 查询 ----------

    def title_of(self, aid):
        """投稿列表里这个 aid 的标题(找不到返回 None)."""
        for v in self.videos:
            if v.get("aid") == aid:
                return v.get("title", "")
        return None

    def video_of(self, aid):
        for v in self.videos:
            if v.get("aid") == aid:
                return v
        return None

    def is_done(self, aid):
        return aid in self.record

    def is_skipped(self, aid):
        return aid in self.skip

    def pending(self):
        """还没下、也没被跳过的视频(按投稿列表顺序)."""
        return [v for v in self.videos
                if v.get("aid") and v["aid"] not in self.record
                and v["aid"] not in self.skip]

    def titles(self):
        """aid -> title 的对照表(日志里把 aid 换成标题用)."""
        return {v.get("aid"): v.get("title", "") for v in self.videos}

    def duplicate_titles(self):
        """出现多次的标题集合.

        同一个标题出现在多个视频上时, "本地是否有这个文件"只能靠精确匹配
        (标题_aid), 不能靠前缀猜, 所以这里先算出来交给 media 模块。
        """
        from .util import sanitize_name

        counts = {}
        for v in self.videos:
            t = sanitize_name(v.get("title", ""))
            counts[t] = counts.get(t, 0) + 1
        return {t for t, c in counts.items() if c > 1}

    # ---------- 同步基线 ----------

    def api_count(self):
        return to_int(self.meta.get(M_TOTAL))

    def full_time(self):
        from .util import parse_stamp
        return parse_stamp(self.meta.get(M_FULL_TIME))

    def is_list_complete(self):
        return self.meta.get(M_COMPLETE) is True

    def collection_spec(self):
        """合集/系列文件夹: 从同步信息里读回 类型/mid/编号.

        读不出来返回 None —— 手写进来的文件夹不该被瞎猜成某个合集。
        """
        kind = self.meta.get(M_KIND)
        if kind not in ("合集", "系列"):
            return None
        mid = self.meta.get(M_MID)
        coll_id = self.meta.get(M_COLL_ID)
        if not mid or not coll_id:
            return None
        return {
            "类型": kind,
            "mid": str(mid),
            "id": str(coll_id),
            "名称": self.meta.get(M_NAME) or str(coll_id),
        }

    def build_up_meta(self, videos, count, complete=True):
        """UP主式基线."""
        return {
            M_LAST_AID: videos[0]["aid"] if videos else None,
            M_TOTAL: count,
            M_COMPLETE: bool(complete),
            M_FULL_TIME: now_str(),
            M_MODE: "全量",
        }

    def build_collection_meta(self, spec, count, mode=None):
        """合集/系列式基线."""
        return {
            M_KIND: spec["类型"],
            M_MID: spec["mid"],
            M_COLL_ID: spec["id"],
            M_NAME: spec["名称"],
            M_TOTAL: count,
            M_COMPLETE: True,
            M_FULL_TIME: now_str(),
            M_MODE: mode or ("%s接口" % spec["类型"]),
        }

    # ---------- 改 ----------

    def mark_done(self, aid, title, bvid="", when=None):
        """登记一个下成功的视频, 并顺手清掉它的失败/跳过记录."""
        self.record[aid] = {
            "title": title,
            "bvid": bvid,
            "time": when or now_str(),
        }
        self.failed.pop(aid, None)
        self.skip.pop(aid, None)

    def mark_failed(self, aid, title, when=None, reason=""):
        """记一次失败. 返回累计失败次数.

        次数到 2 就移进「跳过」, 由调用方决定(见 skip_after_failures)——
        因为"连续失败 2 次"和"被限流导致的失败"要区别对待。

        reason: 失败原因(从 BBDown 输出里认出来的, 见 failures 模块)。
        以前这里只记 title/count/time, 于是事后**完全查不出**当初为什么失败 ——
        实测排查一次"反复判定限流"翻了三个日志文件才还原现场。
        """
        entry = self.failed.get(aid) or {"title": title, "count": 0}
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["time"] = when or now_str()
        if reason:
            entry["原因"] = reason
        self.failed[aid] = entry
        return entry["count"]

    def move_failed_to_skip(self, aid, reason_time=None):
        """把「失败」里的条目移进「跳过」(累计失败 2 次的处理).

        「失败」里的计数只增不减、下载成功才清零, 所以这里是"累计"不是
        "连续" —— 文案和实现必须一致, 免得看日志的人按错的意思排障。

        原因一并带过去: 以后想知道"这个视频为什么被跳过"就不用再翻日志了。
        """
        entry = self.failed.pop(aid, None)
        entry = entry or {}
        title = entry.get("title", "")
        self.skip[aid] = {"title": title,
                          "time": reason_time or entry.get("time") or now_str()}
        if entry.get("原因"):
            self.skip[aid]["原因"] = entry["原因"]
        return self.skip[aid]

    def force_skip(self, aid, title, note):
        """直接写进「跳过」并给出来由(充电专属 / 守护永久失败)."""
        self.skip[aid] = {"title": title, "time": note}

    def add_to_list(self, video):
        """把一个新视频插到名单最前面(单视频下载用), 已存在则不动."""
        if not any(v.get("aid") == video.get("aid") for v in self.videos):
            self.videos.insert(0, video)
            return True
        return False

    def drop_ghost_records(self, exists_fn):
        """删掉"有已下载记录、本地却没有文件"的记录, 让它们重新变回待下载.

        exists_fn(aid, title) -> bool。返回被删掉的 aid 列表。
        """
        ghosts = []
        titles = self.titles()
        for aid in list(self.record):
            title = titles.get(aid)
            if title is None:
                continue          # 已经不在名单里了(UP主删了), 记录留着不动
            if not exists_fn(aid, title):
                del self.record[aid]
                ghosts.append(aid)
        return ghosts


# ---------------- 底层文件读写 ----------------

def _as_dict(value):
    return value if isinstance(value, dict) else {}


def _as_list(value):
    return value if isinstance(value, list) else []


def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _load_state_json(path):
    """读状态文件 -> (数据, 是否"文件在但读不出来").

    三种情况必须分开: 文件不存在(正常, 第一次跑) / 文件在但内容坏了(要留证,
    绝不能被当成空状态盖掉) / 读不了(被占用等, 更不许动它)。
    """
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f), False
    except FileNotFoundError:
        return None, False
    except ValueError:
        return None, True          # 内容不是合法 JSON(含编码不对)
    except OSError:
        return None, False         # 读不了就当没有, 但一个字节都不动它


def _quarantine_broken(path):
    """状态文件坏了 -> 改个名留在原地, 并大声报出来.

    不做这件事的话, 坏文件会和"文件不存在"一样被当成空状态, 紧接着任何一次
    save 都会拿空记录把它盖掉 —— 一个文件夹几万条「已下载」就这么没了。
    改名之后文件还在, 手工能捞回来。
    """
    try:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return
        bad = "%s.bad-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
        os.replace(path, bad)
        _warn("状态文件内容读不出来, 已改名保留为 %s —— 请检查是不是被"
              "手工改坏或磁盘出错" % os.path.basename(bad))
    except OSError:
        pass


def _warn(msg):
    """打一行警告(局部导入, 免得 state 和 logging 循环 import)."""
    try:
        from .logging import log
        log(msg)
    except Exception:
        pass


def _write_json_atomic(path, data, retries=3):
    """原子写 JSON. 返回是否成功."""
    tmp = path + ".tmp"
    try:
        text = json.dumps(data, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return False
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    for _ in range(retries):
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())   # 先落盘再改名: 断电不会留下空文件
            os.replace(tmp, path)
            return True
        except (OSError, UnicodeError):
            # UnicodeError: 标题里混进孤立代理字符时 f.write 抛的是它(不是
            # OSError), 以前会一路冒出去把整个 UP 主的下载打断
            time.sleep(0.5)
    return False


def load_json(path, default=None):
    """读任意 JSON(守护的永久失败名录也用这个); 读不到给 default."""
    data = _read_json(path)
    return default if data is None else data


def save_json_atomic(path, data):
    return _write_json_atomic(path, data)


def cleanup_temp(folder):
    """清掉上次异常退出留下的 .tmp, 免得被误当成正式文件."""
    try:
        tmp = paths.state_file(folder) + ".tmp"
        if os.path.exists(tmp):
            os.remove(tmp)
    except OSError:
        pass


def remove_legacy_files(folder):
    """迁移完成后删掉老格式文件(只在迁移成功时调用)."""
    for name in (paths.LEGACY_LIST_NAME, paths.LEGACY_RECORD_NAME,
                 paths.LEGACY_SKIP_NAME,
                 paths.LEGACY_RECORD_NAME + ".tmp",
                 paths.LEGACY_SKIP_NAME + ".tmp"):
        try:
            path = os.path.join(folder, name)
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


# ---------------- 几个独立的小读写 ----------------

def read_meta(folder):
    """只读同步信息, 读不到给 {}."""
    data = _read_json(paths.state_file(folder))
    if not isinstance(data, dict):
        return {}
    meta = data.get(M_META)
    return meta if isinstance(meta, dict) else {}


def record_count(folder):
    """这个文件夹记了多少个已下载(菜单列表里显示用)."""
    data = _read_json(paths.state_file(folder))
    if isinstance(data, dict):
        record = data.get(S_RECORD)
        return len(record) if isinstance(record, dict) else 0
    legacy = _read_json(os.path.join(folder, paths.LEGACY_RECORD_NAME))
    return len(legacy) if isinstance(legacy, dict) else 0


def pending_summary(folder):
    """读一次状态文件, 把排序要的几个数一次算出来.

        count         还差多少条(名单里有、既没下过也没跳过)
        seconds       这些待下载的视频里**已知时长**的合计(秒)
        known         上面那个合计是几条的
        list_seconds  名单里**所有**知道时长的视频合计(含已下载的)
        list_known    上面那个合计是几条的

    为什么要后两个: "这份名单里一个视频大概多长"用整份名单来算最实在 ——
    刚做完完整校验的名单时长是全的, 只做过增量同步的名单只有新视频有;
    待下载的那部分单拎出来往往样本太少(甚至一条都没有)。
    时长来自列表接口(videos 条目里的 "duration", 见 bilitools), 老状态文件里
    没有就是 0, 排序那边会退回"按条数排"。
    """
    data = _read_json(paths.state_file(folder))
    blank = {"count": 0, "seconds": 0, "known": 0,
             "list_seconds": 0, "list_known": 0}
    if not isinstance(data, dict):
        return dict(blank)
    record = _as_dict(data.get(S_RECORD))
    skip = _as_dict(data.get(S_SKIP))
    # 名单条目也可能是手工改坏的(字符串/数字)。这个函数守护每轮都调,
    # 崩了就是整轮作废, 所以先确认它是个对象。
    out = dict(blank)
    for v in _as_list(data.get(S_VIDEOS)):
        if not isinstance(v, dict) or not v.get("aid"):
            continue
        length = to_int(v.get("duration"), 0) or 0
        if length > 0:
            out["list_seconds"] += length
            out["list_known"] += 1
        aid = v["aid"]
        if aid in record or aid in skip:
            continue
        out["count"] += 1
        if length > 0:
            out["seconds"] += length
            out["known"] += 1
    return out


def pending_count(folder):
    """这份名单大概还差多少个(只读记录不扫文件) —— 用来给任务排序."""
    return pending_summary(folder)["count"]


def ensure_single_list(folder):
    """读"单视频下载"的状态, 顺便把"只有已下载记录、没有名单"的老数据补进名单.

    早期的单视频只写了「已下载」没写名单, 补上之后守护才能一起检查/重试。
    返回 (名单, 记录, 跳过, 失败, 同步信息) —— 保持老调用点的形状。
    """
    if not os.path.isdir(folder):
        return [], {}, {}, {}, {}
    state = State.load(folder)
    added = 0
    known = {v.get("aid") for v in state.videos}
    for aid, entry in state.record.items():
        if aid in known:
            continue
        entry = entry or {}
        state.videos.append({"aid": aid, "bvid": entry.get("bvid", ""),
                             "title": entry.get("title", "")})
        added += 1
    if added:
        state.save()
    return state.videos, state.record, state.skip, state.failed, state.meta


def backup_stamp():
    """给人工看的备份时间戳(排查问题时用)."""
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
