# -*- coding: utf-8 -*-
"""跑一轮: 起一个下载管理器, 盯它直到结束或被判定限流.

守护的核心循环每次只做一件事: 把管理器当子进程拉起来, 一边收它的输出一边
判断"是不是被限流了"。判断有两个来源:

  事件流(首选)  管理器往 --event-log 指定的文件里写一行一条 JSON, 措辞怎么改
                都不影响统计 —— 这是"统计不准"的根因解决方案。
  文本兜底      老版本管理器不认 --event-log 时会退回解析中文日志, 所以升级
                过程中不会因为版本不一致而算错。

"被限流"的判定(和以前一致):
  · 同一轮里连续 FAIL_BURST 个视频解析/下载失败;
  · 或者失败总数超过 FAIL_TOTAL_SOFT 且比成功数还多。
充电/付费专属的失败不算限流(那类永远下不了, 见 maintenance)。

下载期间会定时写"心跳": 还在跑吗、在跑哪个、跑了多久、有没有在长个儿。
BBDown 的进度是回车刷新的, 日志里看不到, 所以只能靠"文件夹变大了多少"来证明
它真的在下载而不是卡死。
"""

import json
import os
import queue
import re
import subprocess
import threading
import time

from . import bilitools, failures, paths
from .logging import decode_line, log, strip_ansi
from .state import State
from .util import clean_text, elapsed_text, now_str, size_text

# 连续失败多少个视频 => 判定被限流
FAIL_BURST = 4
# 每一轮失败太多(即使不连续)也按限流处理
FAIL_TOTAL_SOFT = 8

# 心跳: 每隔多久写一行"我还活着"
HEARTBEAT_SECONDS = 45
# 单个视频超过这么久, 点名提醒(下得慢/可能卡)
SLOW_VIDEO_SECONDS = 300
# 完全没有任何输出这么久, 提示一下(可能是接口卡住)
SILENCE_WARN_SECONDS = 120


class RoundResult(object):
    """一轮的结果. 守护主循环只认这些字段."""

    __slots__ = ("killed_reason", "consec_fail", "max_consec_fail", "fails",
                 "downloads", "attempts", "timeouts", "login_dead", "lock_busy",
                 "unplayable", "last_failed_aid", "exit_code", "finished",
                 "elapsed", "last_video_took", "stop_requested",
                 "consec_by_kind", "fail_kinds", "non_limit_fails",
                 "last_fail_note")

    def __init__(self):
        self.killed_reason = None     # 非 None = 判定被限流, 值是原因
        self.consec_fail = 0          # 连续"真被拦"的个数(别的失败不算)
        self.max_consec_fail = 0
        self.fails = 0
        self.downloads = 0
        self.attempts = 0
        self.timeouts = 0
        self.login_dead = False
        self.lock_busy = False
        self.unplayable = 0
        self.last_failed_aid = None
        self.exit_code = None
        self.finished = False
        self.elapsed = 0.0
        self.last_video_took = None
        self.stop_requested = False
        self.consec_by_kind = {}      # {"风控": 2} —— 按原因分别连数
        self.fail_kinds = {}          # {"多线程": 5, "网络": 3} —— 本轮原因分布
        self.non_limit_fails = 0      # 不是限流、但也失败了的个数
        self.last_fail_note = ""      # 最后一条失败原因的人话

    def summary(self):
        bits = ["成功 %d" % self.downloads, "失败 %d" % self.fails]
        if self.timeouts:
            bits.append("其中卡死超时 %d" % self.timeouts)
        if self.unplayable:
            bits.append("充电专属跳过 %d" % self.unplayable)
        if self.fail_kinds:
            # 把原因分布说出来 —— 这是"为什么失败"最直接的答案
            bits.append("原因: " + ", ".join(
                "%s %d" % (k, v) for k, v in
                sorted(self.fail_kinds.items(), key=lambda kv: -kv[1])))
        return ", ".join(bits)


def heartbeat_text(round_no, round_elapsed, result, active, now, progress=None):
    """一行心跳, 回答"它还在跑吗? 在跑哪个? 跑了多久?".

    这是**单行**版本(旧调用点和测试都按单行用)。要看人的话用
    heartbeat_lines() —— 它拆成两行, 免得在窄窗口里折行。
    """
    return "; ".join(heartbeat_lines(round_no, round_elapsed, result, active,
                                     now, progress))


def heartbeat_lines(round_no, round_elapsed, result, active, now, progress=None):
    """心跳, 拆成 1~2 行. 返回字符串列表.

    为什么拆(用户反馈的一个"看着像缺字"的问题):
        原来所有东西拼成一行, 窄一点的窗口就会在宽度处**折行**, 于是
        "朴孝敏(已 26秒)" 被切成 "…朴孝敏(已 26秒" + 下一行 ")" —— 看起来
        像日志缺字; 更长的还会被窗口直接截掉(截图里就截掉了)。
        拆开之后: 第一行只放数字(短、稳), 第二行才放标题(长、会折也不影响
        看数字)。这样即使折行, 折的也是标题那部分, 关键信息永远完整。
    """
    parts = ["第 %d 轮运行中 %s" % (round_no, elapsed_text(round_elapsed)),
             "成功 %d" % result.downloads,
             "失败 %d" % result.fails]
    if result.timeouts:
        parts.append("超时掐掉 %d" % result.timeouts)
    if result.unplayable:
        parts.append("充电专属跳过 %d" % result.unplayable)
    if active:
        parts.append("在下的 %d 个" % len(active))
    lines = ["; ".join(parts)]
    if active:
        running = sorted(active.items(), key=lambda kv: kv[1][1])
        who = "、".join("%s(已 %s)" % (entry[0][:20], elapsed_text(now - entry[1]))
                        for _aid, entry in running[:2])
        if len(active) > 2:
            who += " 等"
        tail = "在下的: " + who
        if progress:
            tail += "; " + progress
        lines.append(tail)
    else:
        # 这句别删: 它区分"在下小文件/没动静"和"真的什么都没干"。
        # 第二行, 所以再长也不会把上面的数字挤到折行。
        lines.append("当前没有在下的视频(正在刷新名单/准备下一批)"
                     + ("; " + progress if progress else ""))
    return lines


# ---------------- 事件读取 ----------------

def read_new_events(path, state, sink):
    """把事件文件里上次之后的新行读出来(只推进到完整行, 免得读到半行)."""
    try:
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(state.get("pos", 0))
            while True:
                line = f.readline()
                if not line:
                    break
                if not line.endswith("\n"):
                    break               # 半行, 等下次再读
                state["pos"] = f.tell()
                line = line.strip()
                if line:
                    sink(line)
    except (OSError, ValueError):
        pass


class EventLine(object):
    """事件文件里的一行(和子进程 stdout 严格区分开).

    事件和 stdout 以前共用一个队列、靠 "\x00EVENT\x00" 前缀区分 —— 远端
    标题里只要带上 NUL 就能伪装成事件(video_end/run_end), 干扰限流判定。
    现在靠类型区分: stdout 行永远是普通字符串, 不可能被当成事件。
    """

    __slots__ = ("line",)

    def __init__(self, line):
        self.line = line


def _archive_line(line):
    """写进每轮日志的文本: 保留换行, 其它控制字符(NUL/ESC)替换掉.

    行尾的 \r 要先摘掉再清洗: 子进程在 Windows 上输出的是 CRLF, 而
    clean_text 会把 \r 也换成下划线, 于是每行尾巴都挂一个 "_"。
    """
    body = line.rstrip("\r\n")
    text = "\n".join(clean_text(part) for part in body.split("\n"))
    return text + "\n" if line.endswith(("\n", "\r")) else text


def _tail_events(path, sink, stop, state, lock=None, interval=0.5):
    """后台线程: 一直把事件文件的新行搬进队列, 直到 stop 被置位.

    lock 和收尾那一次的补读共用 —— 两边都在动 state["pos"], 不加锁就会把
    最后几行各读一次、各投一次队列, 事件被重复计数。
    """
    def to_queue(line):
        sink(EventLine(line))

    while not stop.is_set():
        if lock is None:
            read_new_events(path, state, to_queue)
        else:
            with lock:
                read_new_events(path, state, to_queue)
        stop.wait(interval)


def _reader(stream, sink):
    """把子进程的输出搬到队列里(逐行两种编码都试, 见 logging.decode_line)."""
    try:
        for raw in iter(stream.readline, b""):
            sink(decode_line(raw))
    except Exception:
        pass
    finally:
        sink(None)


# ---------------- 管理器能力探测 ----------------

def manager_supports_events(script=None):
    """管理器会不会写事件流.

    以前是靠"读管理器源码里有没有 --event-log 这个字符串"来猜 —— 重构一次
    字符串就没了, 守护会悄悄退回解析中文日志。现在改成看它 import 的是不是
    本包: 用本包的管理器一定支持事件流, 这个判断不会因为改文案而失效。
    """
    script = script or paths.manager_script()
    try:
        with open(script, "r", encoding="utf-8", errors="ignore") as f:
            source = f.read()
    except OSError:
        return False
    if "bbdown_kit" in source:
        return True
    # 老版本: 只能看它认不认这个参数(升级期间的兜底)
    return "--event-log" in source


# ---------------- 一轮 ----------------

def run_round(manager_cmd, round_no, quarantined, log_dir, round_log_path,
              events_path, status_writer=None, status_prefix=None,
              tick_hook=None, timeout_minutes=30, manager_path=None,
              log_fn=None, grace_seconds=None):
    """跑一轮, 返回 (round_start, RoundResult).

    manager_cmd      要执行的完整命令(list)
    log_dir          这一轮的日志目录
    round_log_path   人类可读输出存到哪
    events_path      管理器写的 .jsonl 事件文件
    status_writer    回调(lines_list) 用来刷新 守护状态.txt
    tick_hook        每秒回调一次, 返回 True 表示"该停了"; 具体是立刻掐还是
                     优雅收工, 由 grace_seconds 决定(见下面)
    manager_path     这次真正拉起的管理器脚本路径(决定要不要按事件流统计)
    log_fn           这一轮的日志往哪写(默认打控制台)。守护传入"同时写
                     守护日志.txt"的那个函数, 否则心跳/限流判定这些排错
                     最要紧的行只在控制台里, 窗口一关就没了。
    grace_seconds    None = 立刻掐(接管/让位); 数字 = 优雅停止的宽限期:
                     tick_hook 说停之后, 给管理器这么多秒把手上的下完,
                     超时才兜底掐掉。

    注意 manager_path 必须是"这次实际跑的那个管理器", 不能用默认路径去猜 ——
    猜错了会在"管理器不写事件"时静默地一个字都不统计。
    """
    # 本轮所有日志都走这个出口(下面各处沿用裸 log(...) 的写法)。
    # 注意右边必须引用模块级的那个 log: 一旦在函数里给 log 赋值, Python 就把它
    # 当成局部变量, 写成 "log = log_fn or log" 会直接 UnboundLocalError。
    log = log_fn if log_fn is not None else globals()["log"]
    os.makedirs(log_dir, exist_ok=True)
    try:
        if os.path.exists(events_path):
            os.remove(events_path)
    except OSError:
        pass
    use_events = manager_supports_events(manager_path or paths.manager_script())
    round_start = now_str()
    result = RoundResult()
    started = time.time()
    live = {
        "active": {},          # aid -> (标题, 开始时间, 文件夹)
        "order": [],           # 老版本(没有事件流)时用来配对开始/结束
        "last_output": started,
        "last_beat": started,
        "last_bytes": 0,       # 上次心跳时, 在下的那几个文件夹一共多大
        "last_bytes_ts": started,
        "last_silence_note": 0.0,
        "warned": set(),       # 已经点名过的慢视频
    }
    current_folder = ""
    folder_maps = {}
    # 优雅停止的计时起点(用列表包一层, 好让闭包里的 tick 改它)
    drain_started = [None]

    def refresh_status(note):
        if status_writer and status_prefix is not None:
            status_writer(list(status_prefix) + ["", note])

    def on_success():
        result.downloads += 1
        result.consec_fail = 0
        result.consec_by_kind.clear()

    def on_failure(title, aid, kind="", note=""):
        """一个视频失败了.

        这里是这次事故的修复点。以前只要连着失败 4 个就判"被限流", **完全不看
        失败原因** —— 而实测 09-25 那批失败全是 CDN 的毛病:
            服务器可能并不支持多线程下载, 请使用 --multi-thread false 关闭多线程
            下载出现异常, 3秒后将进行自动重试...
        于是并发被降到 1、每 40 秒掐一次进程, 空转了 76 分钟一个也没下成,
        而同一个接口的探测返回码一直是 0(放行)。

        现在:
          · 只有"真的被拦"(风控/限速)才累计 -> 够数就判定限流休息
          · 别的失败(多线程/网络/解析/残缺)只记数、记原因, **不休息**
          · 连着好几个"不是限流"的失败也是一种信号, 但处理方式是让管理器
            自己降速, 而不是整个停下来
        """
        if aid and bilitools.video_unplayable(aid):
            result.unplayable += 1
            _quarantine_charging(aid, title, quarantined, current_folder, log)
            return
        result.fails += 1
        result.last_failed_aid = aid
        result.fail_kinds[kind or failures.UNKNOWN] = (
            result.fail_kinds.get(kind or failures.UNKNOWN, 0) + 1)
        if note and not result.last_fail_note:
            result.last_fail_note = note

        if failures.should_rest(kind):
            # 真被拦了: 按原因分别连数(风控和限速不是一回事)
            result.consec_by_kind[kind] = result.consec_by_kind.get(kind, 0) + 1
            result.consec_fail += 1
            result.max_consec_fail = max(result.max_consec_fail, result.consec_fail)
            if result.consec_fail >= FAIL_BURST:
                result.killed_reason = "%s, 连续失败 %d 个" % (kind,
                                                            result.consec_fail)
        else:
            # 不是限流: 计数清零(它不该被算进"被拦了"的证据链)
            result.consec_fail = 0
            result.consec_by_kind.clear()
            result.non_limit_fails += 1

    def handle_event(raw):
        """处理一条事件; 返回 True 表示确实是事件(不是普通日志)."""
        try:
            event = json.loads(raw)
        except ValueError:
            return True
        kind = event.get("kind")
        for key in ("aid", "title", "folder", "reason"):
            if isinstance(event.get(key), str):
                event[key] = clean_text(event[key])
        if kind == "video_end":
            live["active"].pop(str(event.get("aid")), None)
            if event.get("ok") and not event.get("timeout"):
                # timeout 要先看: 管理器把"超时"和"ok"同时标上时(它可能先
                # 发现文件、后判定超时), 这是被掐掉的下载, 不是成功
                on_success()
                if event.get("elapsed"):
                    result.last_video_took = int(event["elapsed"])
            elif event.get("timeout"):
                # 卡死被管理器掐掉的: 算失败, 但不算"被限流"的连续失败
                result.fails += 1
                result.timeouts += 1
                result.last_failed_aid = event.get("aid")
                log("卡住超时被结束(下次重试): %s [%s]"
                    % (event.get("title", ""), event.get("aid", "")))
            else:
                on_failure(event.get("title", ""), event.get("aid"),
                           event.get("reason", ""), event.get("reason_text", ""))
        elif kind == "video_start":
            result.attempts += 1
            live["active"][str(event.get("aid"))] = (
                event.get("title", ""), time.time(), event.get("folder", ""))
        elif kind == "video_skipped":
            live["active"].pop(str(event.get("aid")), None)
            log("管理器把 %s [%s] 加入跳过 (%s)"
                % (event.get("title", ""), event.get("aid", ""),
                   event.get("reason", "")))
        elif kind == "up_end":
            if event.get("downloaded") or event.get("failed"):
                log("· %s: 成功 %s 失败 %s"
                    % (os.path.basename(event.get("folder", "")),
                       event.get("downloaded", 0), event.get("failed", 0)))
        elif kind == "login_failed":
            result.login_dead = True
        elif kind == "lock_busy":
            result.lock_busy = True
        elif kind == "run_end":
            result.finished = True
        return True

    def handle_text(clean):
        """中文日志兜底(老版本管理器不写事件时)."""
        nonlocal current_folder
        m = re.search(r"开始下载: (.+?) \[(\d+)\]", clean)
        if m:
            title = clean_text(m.group(1))
            live["order"].append((m.group(2), title))
            live["active"][m.group(2)] = (title, time.time())
        m = re.search(r"处理(?:UP主|合集|系列):\s*(.+)$", clean)
        if m:
            current_folder = clean_text(m.group(1).strip())
        elif re.search(r"失败 ✗", clean):
            m = re.search(r"失败 ✗ (.+?) \(", clean)
            title = clean_text(m.group(1).strip()) if m else ""
            if live["order"]:
                done_aid, _title = live["order"].pop(0)
                live["active"].pop(done_aid, None)
            if current_folder not in folder_maps:
                folder_maps[current_folder] = _folder_title_aids(current_folder)
            on_failure(title, folder_maps[current_folder].get(title))
        elif re.search(r"完成 ✓", clean):
            if live["order"]:
                done_aid, _title = live["order"].pop(0)
                live["active"].pop(done_aid, None)
            on_success()
        elif "判定为被限流" in clean:
            result.killed_reason = "管理器自己判定被限流"
        elif "未登录或登录已失效" in clean:
            result.login_dead = True
        elif "检测到另一个下载管理器正在运行" in clean:
            result.lock_busy = True
        elif "全部任务结束" in clean:
            result.finished = True

    def tick(now):
        """每秒叫一次. 返回 True = 中断本轮.

        tick_hook 的语义分两种(由调用方决定):

        · **立刻停**(接管/让位): 返回 True 就当场中断, 收掉进程树。
        · **优雅停**(用户按了停止): 返回 True 只是"不再开新任务"的信号。
          这时候绝不能中断 —— 在下的那几个还没下完, 掐掉就留下半成品,
          正是优雅停止想避免的。所以给它一个宽限期(grace_seconds),
          让 manager 自己把手上那批跑完、正常退出; 真卡住了才兜底掐掉。
        """
        if tick_hook is not None and tick_hook():
            result.stop_requested = True
            if grace_seconds is None:
                result.killed_reason = "收到停止请求(另一个守护接管)"
                log("收到另一个守护的停止请求, 本守护收摊让位")
                return True
            if drain_started[0] is None:
                drain_started[0] = now
                log("收到优雅停止请求: 不再开新任务, 等在下的 %d 个下完"
                    % len(live["active"]))
            waited = now - drain_started[0]
            if waited >= grace_seconds:
                result.killed_reason = ("优雅停止超时(等了 %s 还没下完)"
                                        % elapsed_text(waited))
                log("!! 优雅停止等了 %s 还没下完, 兜底结束它(这次会留下半成品, "
                    "由下一次启动时的残留清理收掉)" % elapsed_text(waited))
                return True
            # 宽限期内: 继续陪跑, 只是不再提交新任务(管理器那边会看到同一个
            # 请求文件并停止取新视频)
        if now - live["last_beat"] >= HEARTBEAT_SECONDS:
            _beat(now)
        _warn_slow(now)
        if (now - live["last_output"] >= SILENCE_WARN_SECONDS
                and now - live["last_silence_note"] >= SILENCE_WARN_SECONDS):
            live["last_silence_note"] = now
            log("… 已经 %s 没有新日志了 (在下 %d 个视频). BBDown 的进度是回车"
                "刷新的, 只有下完/失败才写一行, 所以这几分钟安静是正常的; "
                "看上面的'落盘 +xx MB'就知道在不在长"
                % (elapsed_text(now - live["last_output"]), len(live["active"])))
        return False

    def _beat(now):
        from .media import folder_bytes

        folders = {entry[2] for entry in live["active"].values()
                   if len(entry) > 2 and entry[2]}
        now_bytes = sum(folder_bytes(f) for f in folders)
        progress = None
        if folders and live["last_bytes"]:
            delta = now_bytes - live["last_bytes"]
            secs = max(1.0, now - live["last_bytes_ts"])
            if delta > 0:
                progress = "落盘 +%s(%.0f 秒, 约 %s/s)" % (
                    size_text(delta), secs, size_text(delta / secs))
            else:
                progress = "这 %.0f 秒没长个儿(可能在下小文件或真的卡了)" % secs
        live["last_bytes"], live["last_bytes_ts"] = now_bytes, now
        live["last_beat"] = now
        # 拆成两行写: 数字一行(短), 在下的另起一行(标题长, 折行也不影响数字)
        hb_lines = heartbeat_lines(round_no, now - started, result,
                                   live["active"], now, progress)
        for item in hb_lines:
            log(item)
        refresh_status("状态: " + "; ".join(hb_lines))

    def _warn_slow(now):
        for aid_key, entry in sorted(live["active"].items(),
                                     key=lambda kv: kv[1][1]):
            title, video_started = entry[0], entry[1]
            if now - video_started < SLOW_VIDEO_SECONDS or aid_key in live["warned"]:
                continue
            live["warned"].add(aid_key)
            extra = ""
            if timeout_minutes:
                extra = (" (管理器设的单视频上限 %d 分钟, 到了会掐掉下次重试)"
                         % timeout_minutes)
            log("⚠ 下得慢/可能卡住: %s [%s] 已经 %s%s"
                % (title, aid_key, elapsed_text(now - video_started), extra))

    with open(round_log_path, "w", encoding="utf-8") as out:
        proc = subprocess.Popen(
            manager_cmd, cwd=paths.PROJ, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0,
        )
        q = queue.Queue()
        threading.Thread(target=_reader, args=(proc.stdout, q.put),
                         daemon=True).start()
        stop_tail = threading.Event()
        tail_state = {"pos": 0}
        tail_lock = threading.Lock()
        tail_thread = threading.Thread(
            target=_tail_events,
            args=(events_path, q.put, stop_tail, tail_state, tail_lock),
            daemon=True)
        tail_thread.start()

        # 从这里开始到进程结束, 中间**任何**异常(Ctrl+C、磁盘满、编码炸)
        # 都必须先把管理器连同它的 BBDown/ffmpeg 收掉再往外抛。
        # 以前只有"正常结束"和"判定限流"两条路会 kill: 用户一按 Ctrl+C,
        # 守护退出(130)、两把锁被系统放掉, 而管理器还在后台继续下载 ——
        # 变成"没有锁的下载", 下一次启动还会再起一个, 而且再也没人管它。
        interrupted = False
        try:
            while True:
                if tick(time.time()):
                    break
                try:
                    line = q.get(timeout=1.0)
                except queue.Empty:
                    if proc.poll() is not None and q.empty():
                        break
                    continue
                if line is None:
                    break
                live["last_output"] = time.time()

                if isinstance(line, EventLine):
                    handle_event(line.line)
                    if result.killed_reason:
                        break
                    continue

                # 人类可读日志: 始终存档, 但走事件流时不再拿它计数
                out.write(_archive_line(line))
                out.flush()
                clean = strip_ansi(line).rstrip("\r\n")
                if not clean or use_events:
                    continue
                handle_text(clean)
                if result.killed_reason:
                    break
        except BaseException:
            # 注意: 这里不能靠 finally 里 poll() 的结果判断"要不要收" ——
            # 子进程刚退出时 poll() 可能还是 None, 正常跑完的一轮也会被当成
            # "还在跑", 于是每次正常结束都白打一行"收掉它: 无"。
            interrupted = True
            raise
        finally:
            if interrupted:
                stop_tail.set()
                _reap(proc, "本轮被中断(Ctrl+C/异常), 收掉管理器")

        if result.killed_reason:
            _reap(proc, result.killed_reason)
        else:
            try:
                result.exit_code = proc.wait(timeout=10)
            except Exception:
                _reap(proc, "收尾超时")

    # 收尾: 管理器退得快时最后几条事件可能还没被读到
    # 必须先等 tail 线程真的退出再自己补读一遍: 两边同时读同一个 tail_state,
    # 最后几行会被各读一次、各投一次队列, 事件就被重复计数了。
    stop_tail.set()
    tail_thread.join(timeout=3)
    with tail_lock:
        read_new_events(events_path, tail_state,
                        lambda line: q.put(EventLine(line)))
    while True:
        try:
            extra = q.get_nowait()
        except queue.Empty:
            break
        if isinstance(extra, EventLine):
            handle_event(extra.line)
    try:
        if proc.stdout:
            proc.stdout.close()
    except Exception:
        pass
    if use_events and not os.path.exists(events_path):
        # 一直是"可能不准", 但实际是**一个都没统计到**: 走事件流时不再解析
        # 中文日志(见上面的 use_events 分支), 事件文件没写出来就等于这一轮
        # 白统计 —— 结果会是 0 成功 0 失败, 接着被判成"没有进展"。
        log("提醒: 管理器没写出事件文件, 这一轮的成败统计是空的"
            "(这一轮等于白算, 大概率会被当成没有进展)")
    result.elapsed = time.time() - started
    # 判"是不是被限流"要看**非超时**的失败: 卡死超时是我们自己把视频掐掉的,
    # 不是接口在拦。以前把超时也算进来, 于是"一屏视频都卡住"会被当成被限流:
    # 守护白休息十几分钟, 还顺手撤回了本轮的失败计数 —— 那些视频就永远攒不够
    # 失败次数, 永远在重试。
    network_fails = result.fails - result.timeouts
    rest_fails = sum(n for kind, n in result.fail_kinds.items()
                     if failures.should_rest(kind))
    if (not result.killed_reason and network_fails >= FAIL_TOTAL_SOFT
            and network_fails > result.downloads * 0.5
            and rest_fails * 2 >= network_fails):
        # 注意最后那个条件: 光"失败多"不算被限流 —— 还要**多数失败是
        # 真被拦(风控/限速)**才算。以前只看数量, 于是"一批大视频全卡在
        # CDN 上"也会被当成限流, 白休息一轮还撤回失败计数。如果失败原因
        # 全是多线程/网络/解析这类, 就不休息: 让管理器自己降速, 继续跑。
        result.killed_reason = ("失败 %d 个 / 成功 %d 个(其中被拦 %d 个)"
                                % (result.fails, result.downloads, rest_fails))
        log("这一轮失败偏多(%s), 按被限流处理" % result.killed_reason)
    elif not result.killed_reason and rest_fails == 0 and result.fail_kinds:
        log("这一轮失败 %d 个, 但原因都不是限流(%s) —— 不休息, 继续跑"
            % (result.fails, result.summary()))
    return round_start, result


def _reap(proc, why):
    """收掉管理器进程和它的整棵进程树(BBDown / ffmpeg), 然后等它真的退出.

    幂等: 进程已经没了就什么都不做。收尾失败绝不能让整轮崩掉 —— 它只是
    "善后", 抛异常反而会把真正的错误盖掉。
    """
    from .procs import kill_tree

    try:
        if proc.poll() is not None:
            return
    except Exception:
        pass
    try:
        kill_tree(proc.pid, why)
    except Exception as e:
        log("收进程树出错(忽略): %r" % e)
    try:
        proc.wait(timeout=15)
    except Exception:
        pass


def _quarantine_charging(aid, title, quarantined, current_folder, log_fn=None):
    """充电/付费专属: 记进永久失败名录, 不再重试."""
    from .state import save_json_atomic

    emit_log = log_fn or log
    if (quarantined.get(aid) or {}).get("permanent"):
        return
    record = quarantined.get(aid) or {"title": title, "fails": 0}
    record["permanent"] = True
    record["reason"] = "充电/付费专属"
    record["folder"] = os.path.basename(current_folder)
    record["last"] = now_str()
    quarantined[aid] = record
    save_json_atomic(paths.quarantine_file(), quarantined)
    emit_log("充电/付费专属, 永久跳过: %s [%s]" % (title, aid))


def _folder_title_aids(folder_path):
    """从 下载状态.json 里拿 标题 -> aid 的对照表(识别失败的是哪个视频)."""
    st = State.load(folder_path)
    mapping = {}
    for video in st.videos:
        title, aid = video.get("title"), video.get("aid")
        if title and aid:
            mapping.setdefault(title, aid)
    return mapping
