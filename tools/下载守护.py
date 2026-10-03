# -*- coding: utf-8 -*-
"""下载守护: 盯着 BBDown 下载管理器, 被限流就结束进程 -> 休息 -> 重开, 直到名单里的视频都补齐.

用法:
    python 下载守护.py --status        只看进度, 不改任何东西
    python 下载守护.py --audit         深度自检(只读): 三类目录的账 + 会不会有漏
    python 下载守护.py                 开始守护(会先接管正在跑的下载管理器)
    python 下载守护.py --no-takeover   不杀正在跑的进程, 等它自己结束再接管
    python 下载守护.py --force         已经有一个守护在跑时, 强行接管(默认拒绝启动)
    python 下载守护.py --probe-login   只测登录状态(区分风控和真过期)
    python 下载守护.py --probe-api     诊断 nav/view/playurl 三个接口的返回码
    python 下载守护.py --probe-aids 1,2  诊断: 这些视频是不是充电专属

约定:
    · 单实例: 手里的锁是 守护.lock, 只允许一个守护; 残留的锁(进程已死)会自动清掉
    · 日志:   每次启动一个目录 logs\\守护日志\\<启动时间>\\, 里面每轮一个 .txt(人类看)
              和一个 .events.jsonl(机器看); 只保留最近 10 次启动, 旧的自动清掉
              守护日志.txt 超过 8MB 会自动改名存档, 只留最近 3 份
    · 进度:   守护读管理器写的 .events.jsonl 来统计成败; 老版本管理器不认这个
              参数时, 自动退回解析中文日志
    · 不改状态: 只在两轮之间改 下载状态.json, 运行期间绝不动它

判定"被限流": 同一轮里连续 FAIL_BURST 个视频解析/下载失败。
触发后:
    1. 结束整个进程树(下载管理器 + BBDown + ffmpeg)
    2. 把这一轮里因限流被写进"跳过/失败"的视频放回队列(下次重新下载)
    3. 休息一段时间(逐次加长), 降低并发后重新开始
    4. 每一轮开始前做维护: 把"跳过"里的视频放回队列(永久失败的除外),
       把"有记录但本地没文件"的重新变成待下载
    5. 全部补齐后写 守护状态.txt 并退出

这个文件是薄入口: 真正的实现都在 tools\\bbdown_kit\\ 里。

退出码:
    0   全部补齐(或用 --forever 跑到被要求让位)
    1   达到最大轮数退出
    2   需要人工处理(登录过期 / 一直有别的下载管理器在跑)
    3   连续多轮没有进展, 停下来等人工看看
    4   已经有一个守护/管理器在跑, 拒绝启动
    130 被 Ctrl+C 中断
"""

import argparse
import atexit
import os
import re
import shutil
import sys
import time

# 让"直接双击 python 文件"和"被 guard.py 用 runpy 拉起来"两种方式都能 import 本包
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bbdown_kit import analyze, bilitools, config, maintenance, orders, paths
from bbdown_kit import logging as kit_log
from bbdown_kit import procs, runner, state as state_mod, workdirs
from bbdown_kit.util import elapsed_text, now_str, size_text


def log(msg):
    """守护的每一行日志都同时写进 logs\\守护日志.txt.

    以前这里是 `log = kit_log.log` —— 而 kit_log.log 只有显式传了 to_file
    才会写文件, 于是守护日志.txt 变成死文件(实测最后写入停在 9/29, 而守护
    之后照跑不误): 守护为什么休息、为什么降速、为什么放弃, 全部只存在于
    控制台。计划任务拉起守护时窗口是隐藏的, 等于一点记录都没有, 出事无从查起。
    文件太大时由 rotate_guard_log() 轮换(8MB, 留最近 3 份)。
    """
    line = kit_log.log(msg, to_file=paths.guard_log())
    return line


# ---------------- 参数 ----------------

# 被限流后休息多久(秒), 逐次加长。
# 按你的要求: 5 分钟起步, 一路加长到 20 分钟; 到了 20 分钟还不行就**优雅收工**
# (不再无限等下去)。为什么敢收工: 判定已经改成"只有真被拦才算"(见 failures.py),
# 所以走到这里说明账号确实在被拦, 继续耗着不如把进度存好、等下次再跑 ——
# 大半夜空转反而更容易把限流拖长。
REST_STEPS = [300, 600, 1200]
# 连续这么多轮一点进展都没有 => 停下来提醒人工看看
STALL_ROUNDS = 4
# 限流降速档位: (并发, 间隔秒); 第 0 档由 下载设置.txt 覆盖。
# 这些是"上限", 真正用的是 speed_levels(): 它保证档位只会比用户设的更慢。
SPEED_LEVELS = [(3, 2.0), (2, 3.0), (1, 5.0), (1, 8.0)]
# 同一个 aid 连续探到几次"还在拦"就不再等它了(见 one_round 的注释)
MAX_BLOCK_PROBES = 3


def speed_levels(base_parallel, base_interval):
    """从用户的设置出发算降速档位: 只会越来越慢, 绝不会更激进.

    以前第 1~3 档是写死的绝对值, 于是用户把并行设成 1、间隔设成 10 秒
    (本来就是躲风控)之后, 一旦被判定限流反而变成"并发 2 / 间隔 3 秒" ——
    越限速越猛。这里每一档都以上一档为界取"更慢"的那一边, 保证单调。
    """
    base = (max(1, int(base_parallel)), max(0.0, float(base_interval)))
    levels = [base]
    for parallel, interval in SPEED_LEVELS[1:]:
        p = min(max(1, int(parallel)), levels[-1][0])
        i = max(float(interval), levels[-1][1])
        if p == levels[-1][0] and i == levels[-1][1]:
            # 这一档跟上一档一样慢: 那就把间隔再拉长一截, 保证真的有减速
            i = levels[-1][1] + max(1.0, levels[-1][1])
        levels.append((p, i))
    return levels

# 日志细节: "到底在跑还是卡住了" 靠这几个常量
KEEP_RUN_DIRS = 10              # 只保留最近 N 次启动的日志目录
GUARD_LOG_MAX_BYTES = 8 * 1024 * 1024
GUARD_LOG_KEEP = 3
ORDER_ARG = None                # 命令行 --order: 临时指定"这一轮先处理谁"


def build_parser():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--status", action="store_true",
                    help="只统计缺多少, 不改任何东西")
    ap.add_argument("--audit", action="store_true",
                    help="深度自检(只读): 三类目录各自的账 + 有没有漏在管理之外的东西")
    ap.add_argument("--order", default=None,
                    help="这一轮先处理谁: collection_first(合集优先)/smallest"
                         "(缺口小的优先)/largest/name; 不写就用 下载设置.txt 里的")
    ap.add_argument("--probe-login", action="store_true",
                    help="只测一下登录状态(区分风控和真过期)")
    ap.add_argument("--probe-api", action="store_true",
                    help="诊断 nav/view/playurl 三个接口的返回码, 看是不是被风控")
    ap.add_argument("--probe-aids", default=None,
                    help="逗号分隔的 aid, 看这些视频是不是充电专属")
    ap.add_argument("--clean-workdirs", action="store_true",
                    help="清理下载被掐断后残留的 <aid> 临时目录(默认只列清单)")
    ap.add_argument("--yes", action="store_true",
                    help="配合 --clean-workdirs: 真的删(不加就只是干跑看一眼)")
    ap.add_argument("--freeze", default=None, metavar="UID或文件夹",
                    help="冻结一个名单: 不再更新、不再下载, 但名单原样保留。"
                         "可以给 UID(例如 123456789)或文件夹名")
    ap.add_argument("--unfreeze", default=None, metavar="UID或文件夹",
                    help="启用一个被冻结的名单, 下一轮就会重新检查它的更新")
    ap.add_argument("--freeze-list", action="store_true",
                    help="列出当前被冻结的名单")
    ap.add_argument("--freeze-reason", default="", metavar="原因",
                    help="配合 --freeze: 记一句为什么冻它(可选)")
    ap.add_argument("--freeze-clean", action="store_true",
                    help="清掉冻结名单里已经匹配不上文件夹的失效条目")
    ap.add_argument("--no-takeover", action="store_true",
                    help="不杀正在跑的下载管理器")
    ap.add_argument("--force", action="store_true",
                    help="已经有一个守护在跑时, 强行把它停掉再启动(默认拒绝启动)")
    ap.add_argument("--max-rounds", type=int, default=400)
    ap.add_argument("--forever", action="store_true",
                    help="补齐后不退出, 每隔一段时间再看有没有新视频")
    return ap


# ---------------- 日志目录的整理 ----------------

def rotate_guard_log(stamp):
    """守护日志太大就改名存档, 只留最近几份, 免得无限膨胀."""
    path = paths.guard_log()
    try:
        if not os.path.exists(path):
            return
        if os.path.getsize(path) < GUARD_LOG_MAX_BYTES:
            return
        base, _ext = os.path.splitext(path)
        os.replace(path, "%s.%s.txt" % (base, stamp))
        # 归档就写在日志文件边上(logs\), 清理当然也要看这个目录 ——
        # 以前在 paths.PROJ(tools\) 里找, 永远找不到, "只留最近 3 份"从没生效。
        log_dir = os.path.dirname(path)
        olds = sorted(
            os.path.join(log_dir, name) for name in os.listdir(log_dir)
            if re.match(r"^守护日志\.\d{8}-\d{6}\.txt$", name)
        )
        for old in olds[:-GUARD_LOG_KEEP]:
            try:
                os.remove(old)
            except OSError:
                pass
    except OSError:
        pass


def prune_run_dirs(keep=KEEP_RUN_DIRS):
    """只保留最近几次启动的日志目录(只动 守护日志\\ 里自己建的目录)."""
    root_dir = paths.round_log_dir()
    try:
        dirs = []
        for name in os.listdir(root_dir):
            if not re.match(r"^\d{8}-\d{6}$", name):
                continue
            full = os.path.join(root_dir, name)
            if os.path.isdir(full):
                dirs.append(full)
        dirs.sort()
        root = os.path.realpath(root_dir) + os.sep
        for old in dirs[:-keep] if keep > 0 else dirs:
            real = os.path.realpath(old) + os.sep
            if not real.startswith(root):
                continue        # 保险: 只删自己的日志目录
            shutil.rmtree(old, ignore_errors=True)
    except OSError:
        pass


def _display_path(path):
    """日志里的路径: 能算相对就用相对的, 不同盘符时退回绝对路径.

    (日志目录可以通过设置挪到别的盘, os.path.relpath 跨盘会直接抛异常。)
    """
    try:
        return os.path.relpath(path, paths.ROOT).replace("\\", "/")
    except ValueError:
        return path


def write_clean_list(items):
    """把这次要删的残留目录整份存一份清单(留证/事后想查删了什么).

    写在日志目录里, 和守护日志放一起; 写不进去也不影响清理本身。
    """
    path = os.path.join(paths.log_root(),
                        "清理临时目录-%s.txt" % time.strftime("%Y%m%d-%H%M%S"))
    try:
        with open(path, "w", encoding="utf-8") as f:
            for item in items:
                f.write("%10s  %s\n" % (size_text(item.bytes), item.path))
    except OSError:
        return None
    return path


def clean_workdirs_command(args):
    """手动清理下载残留的 <aid> 临时目录(默认只列清单, 加 --yes 才真删)."""
    if args.yes:
        holder = procs.lock_holder(paths.manager_lock(), paths.manager_who())
        if holder:
            print("现在有下载进程在跑(pid=%s), 不能真删 —— 会把它正在下的目录删掉。"
                  % holder)
            print("先等它结束; 只想看会删什么的话, 去掉 --yes 干跑一遍。")
            return 2
    report = workdirs.sweep(dry_run=not args.yes)
    items = report["items"]
    print("扫描到 %d 个下载残留的临时目录, 共 %s"
          % (report["dirs"], size_text(report["bytes"])))
    for item in items[:20]:
        print("  %10s  %s" % (size_text(item.bytes), _display_path(item.path)))
    if len(items) > 20:
        print("  ... 还有 %d 个" % (len(items) - 20))
    if not args.yes:
        print("这是干跑(什么都没删)。确认要删就加 --yes")
        return 0
    saved = write_clean_list(items)
    if saved:
        print("本次删掉的清单已留存: %s" % _display_path(saved))
    print("已清掉 %d 个, 释放 %s" % (report["cleaned"], size_text(report["freed"])))
    if report["cleaned"] < report["dirs"]:
        print("有 %d 个没删掉(文件可能还被占着), 下一轮或下次运行会再试"
              % (report["dirs"] - report["cleaned"]))
    return 0


def write_status(lines):
    """写 守护状态.txt; 先写临时文件再改名, 免得读到写了一半的内容."""
    path = paths.guard_status()
    text = "\n".join(lines) + "\n"
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError:
        try:                     # 改名失败(别的程序正打开着)就退回直接写
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass


# ---------------- 休息 / 让位 ----------------

def yield_to_new_guard():
    """收到停止请求时的统一收摊动作: 写状态、交还锁、返回退出码."""
    log("已按请求退出, 让新守护接手")
    write_status(["状态: 已让位给新守护 (%s)" % now_str()])
    procs.guard_lock.release()
    procs.manager_lock.release()
    return 0


def sleep_with_status(seconds, note):
    """休息(等限流解除/轮间等待)期间也要响应"停止请求".

    以前这里一睡就是 10 分钟, --force 交接时老守护收不到请求, 会拖到睡醒为止
    (实测踩过). 现在每 2 秒看一眼停止请求, 收到就返回 True 让人收摊。
    """
    end = time.time() + seconds
    while True:
        if procs.stop_requested_for_me():
            log("休息期间收到另一个守护的停止请求, 立刻收摊让位")
            return True
        left = end - time.time()
        if left <= 0:
            return False
        write_status([
            "状态: 休息中(%s)" % note,
            "恢复时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S",
                                        time.localtime(end)),
            "剩余: %.0f 秒" % left,
            "更新时间: %s" % now_str(),
        ])
        time.sleep(min(2, left))


# 休息的入口。默认就是上面的 sleep_with_status; 测试里换掉它就不用真睡。
# (守护有几处一睡就是几分钟到一小时, 测试必须能绕开。)
wait_hook = sleep_with_status


# ---------------- 守护主循环 ----------------

class Guard(object):
    """一次守护运行的全部状态."""

    def __init__(self, args, settings, quarantine, run_dir):
        self.args = args
        self.settings = settings
        self.quarantine = quarantine
        self.run_dir = run_dir
        self.levels = speed_levels(settings.get("parallel", 2),
                                   settings.get("interval", 1.0))
        self.block_probes = 0
        self.level = 0
        self.rest_index = 0
        self.stall = 0
        self.round_no = 0
        self.consecutive_lock_busy = 0
        self.block_aid = None
        self.timeout_minutes = int(settings.get("download_timeout_minutes", 30) or 0)
        self.order = (args.order or "").strip() or settings.get("order") \
            or orders.DEFAULT_ORDER

    # ---- 每一轮 ----

    def manager_command(self, parallel, interval, events_path):
        """守护拉起的下载管理器命令行.

        关于限流: 以前这里写死 `--fail-threshold 99`(等于**关掉**管理器自带的
        "降速 + 冷却"), 理由是"限流判定交给守护"。实测这个分工是错的:
        守护原来的判据是"连续失败 4 个", 不看原因, 于是 CDN 抽风也被当成限流,
        每 40 秒掐一次进程、并发降到 1 —— 管理器那套更温和的机制反而没机会工作
        (见 failures.py 顶部的真实事故记录)。

        现在反过来: **管理器自己管限流**(连续失败到阈值就降并发、加大间隔、
        冷却一段时间再继续), 守护只在拿到"真被拦"的证据时才介入。
        阈值用管理器的默认值(8), 并把冷却时间交给守护统一算 —— 因为守护
        才知道自己已经休息过几次、要不要直接收工。
        """
        cmd = [
            sys.executable, paths.manager_script(), "--all", "--yes",
            "--lock-held",              # 互斥由守护全程代持
            "--pause-seconds", "0",     # 轮间休息由守护统一负责
            "--parallel", str(parallel),
            "--interval", str(interval),
            "--event-log", events_path,
        ]
        if ORDER_ARG:
            cmd += ["--order", ORDER_ARG]
        return cmd

    def stop_signal(self):
        """本轮要不要停.

        两种情况都算"停", 但语义完全不同(由 grace_seconds 区分):
          · 另一个守护要我让位(停止请求里写着我的 pid) -> 立刻收摊
          · 用户按了优雅停止                            -> 在下的下完再退

        注意**必须**在这里报"优雅停止", 否则管理器下完手上的就正常退出了,
        守护看到"一轮正常结束"会接着开下一轮 —— 用户以为停了, 它却还在下。
        """
        if procs.stop_requested_for_me():
            return True
        return procs.graceful_stop_requested()

    def run_one_round(self, status_lines):
        """跑一轮. 返回 (本轮开始时间, RoundResult).

        优雅停止期间(tick_hook)只做一件事: **不杀任何东西**, 只是让这一轮
        不再开新任务。runner 会在被要求停时收尾, 而管理器那边的
        lockstep.run_batch 看到同一个请求文件后就不再取新视频了。
        """
        parallel, interval = self.levels[min(self.level, len(self.levels) - 1)]
        os.makedirs(self.run_dir, exist_ok=True)
        round_log = os.path.join(self.run_dir, "第%03d轮.txt" % self.round_no)
        events_path = os.path.join(self.run_dir,
                                   "第%03d轮.events.jsonl" % self.round_no)
        log("第 %d 轮开始: 并发 %d, 间隔 %.1f 秒, 输出 -> %s"
            % (self.round_no, parallel, interval, _display_path(round_log)))
        return runner.run_round(
            manager_cmd=self.manager_command(parallel, interval, events_path),
            round_no=self.round_no,
            quarantined=self.quarantine,
            log_dir=self.run_dir,
            round_log_path=round_log,
            events_path=events_path,
            status_writer=write_status,
            status_prefix=status_lines,
            tick_hook=self.stop_signal,
            timeout_minutes=self.timeout_minutes,
            manager_path=paths.manager_script(),
            log_fn=log,          # 心跳/限流判定也进 守护日志.txt
            # 优雅停止的宽限期: 用户按了停止之后, 在下的这几个还允许下多久。
            # 取"单视频超时上限 + 2 分钟"——它本来就该在超时之前下完; 真卡住
            # 也有 download_timeout_minutes 兜底, 不在这里干等。
            grace_seconds=(self.timeout_minutes * 60 + 120
                           if self.timeout_minutes else 1800),
        )

    # ---- 轮次之间的处理 ----

    # 休息档位用完之后就不再无限等下去, 改为体面收工。"等过最久的那一档
    # 之后还不行" = 已经按 last 档等过一次了。
    EXIT_CODE_LIMITED = 10

    def rest_budget_used_up(self):
        """休息档位是否已经用完(用完就该收工, 不再空转)."""
        return self.rest_index > len(REST_STEPS)

    def give_up_limited(self, why):
        """限流等不散: 把进度存好、体面收工. 返回退出码(10).

        为什么敢停: 判定已经只看"真被拦"的证据, 走到这里说明账号确实在被拦;
        通宵空转只会把限流拖得更久, 而进度都已经存好了, 下次接着下就行。
        """
        waited = "/".join("%d" % (s // 60) for s in REST_STEPS)
        log("已经按 %s 分钟休息过 %d 次, 限流还没散 —— 优雅收工, 不再空转"
            % (waited, len(REST_STEPS)))
        log("原因: %s" % why)
        log("进度都已保存; 想继续就重新双击 启动下载守护.bat (建议过几小时再试)")
        write_status([
            "状态: 被限流, 已优雅收工 (%s)" % why,
            "已经休息过: %s 分钟 %d 次" % (waited, len(REST_STEPS)),
            "更新时间: %s" % now_str(),
            "",
            "进度都已保存, 不会丢。",
            "过几小时重新双击 启动下载守护.bat 即可继续。",
        ])
        return self.EXIT_CODE_LIMITED

    def step_rest(self, why):
        """休息(逐次加长), 返回 True = 收到停止请求要收摊."""
        rest = REST_STEPS[min(self.rest_index, len(REST_STEPS) - 1)]
        self.rest_index += 1
        level = min(self.level + 1, len(self.levels) - 1)
        self.level = level
        last = self.rest_index >= len(REST_STEPS)
        log("休息 %d 分钟后重开, 并降速到 并发 %d / 间隔 %.1f 秒%s"
            % (rest // 60, self.levels[level][0], self.levels[level][1],
               "; 这是最后一次等待, 还不通就收工" if last else ""))
        return wait_hook(rest, why)

    def handle_rate_limit(self, round_start, result):
        """被判定限流: 撤回本轮写进「跳过」的条目, 休息, 歇不动了就优雅收工."""
        reverted = maintenance.undo_round_damage(round_start, self.quarantine)
        log("判定被限流 (%s): 本轮用时 %s, %s, 撤回跳过 %d 个"
            % (result.killed_reason, elapsed_text(result.elapsed),
               result.summary(), reverted))
        # 只做记录: 这个码要等它自己散, 提前重开只会又撞上
        code = (bilitools.playurl_code(result.last_failed_aid)
                if result.last_failed_aid else None)
        log("现在同一个接口返回码=%s (0=已放行, 87008=还在拦)" % code)
        if result.last_failed_aid:
            self.block_aid = result.last_failed_aid
        # 休息档位用完了(最久那一档也等过了): 不再无限等, 体面收工。
        if self.rest_budget_used_up():
            return self.give_up_limited(result.killed_reason)
        if self.step_rest("被限流, 第 %d 次" % (self.rest_index + 1)):
            return yield_to_new_guard()
        return None

    def handle_clean_round(self, round_start, result):
        """正常结束的一轮: 记失败、收尾判断. 返回退出码或 None."""
        newly = maintenance.record_round_failures(round_start, self.quarantine)
        if newly:
            for aid in newly:
                if aid in self.quarantine:
                    self.quarantine[aid]["permanent"] = True
            log("这些视频反复失败, 标记为永久失败(疑似充电/会员/已删除): %s"
                % ", ".join(newly[:10]))
        stale = maintenance.prune_quarantine(self.quarantine,
                                             maintenance.alive_aids(),
                                             maintenance.done_aids())
        if stale:
            log("永久失败名录里清掉 %d 条已经不在任何名单里的记录" % len(stale))
        state_mod.save_json_atomic(paths.quarantine_file(), self.quarantine)
        if result.downloads > 0 and result.fails == 0:
            if self.level > 0:
                self.level -= 1
                log("这一轮很干净(成功 %d, 0 失败), 提回 并发 %d / 间隔 %.1f"
                    % (result.downloads, SPEED_LEVELS[self.level][0],
                       SPEED_LEVELS[self.level][1]))
            self.rest_index = 0
        return None

    def sweep_workdirs(self):
        """把上一次被掐断的下载留下的 <aid> 临时目录清掉.

        BBDown 正常下完会自己清("清理分片.."/"清理临时文件.."); 只有被我们
        掐断的(限流、超时、接管)才留得下来。它没有断点续传, 这些半成品下次
        重下时它也会先删掉, 留着纯粹是白占磁盘 —— 实测这么攒到过 634 个
        目录 / 90 GB。规则在 bbdown_kit/workdirs.py: 目录名是 aid + 里面全是
        这个 aid 的临时产物 + 至少 10 分钟没动过, 拿不准的一个都不动。

        放在每轮开跑前(此刻没有任何下载在跑): 掐断即清漏掉的(强杀/断电)
        由这里兜底。
        """
        try:
            report = workdirs.sweep()
        except Exception as e:
            log("清理下载残留出错(忽略, 不影响这一轮): %r" % e)
            return
        if report["dirs"]:
            log("清掉 %d 个下载残留的临时目录 (释放 %s)"
                % (report["cleaned"], size_text(report["freed"])))

    def handle_login_dead(self, result):
        """管理器说登录失效: 先分辨"真过期"还是"被风控". 返回退出码或 None."""
        verdict, detail = bilitools.probe_login()
        expiry = bilitools.sessdata_expiry()
        log("管理器说登录失效, 探测结果=%s (%s); cookie 本地过期时间=%s"
            % (verdict, detail, expiry or "读不到"))
        if verdict == "dead":
            log("!! 登录真的过期了, 需要人工扫码 (BBDown-身份登录.bat), 守护停止")
            write_status([
                "状态: !! 登录已过期, 需要人工运行 BBDown-身份登录.bat 扫码",
                "cookie 过期时间: %s" % (expiry or "读不到"),
                "更新时间: %s" % now_str(),
            ])
            return 2
        if self.step_rest("登录接口被风控(%s)" % verdict):
            return yield_to_new_guard()
        return None

    # ---- 主循环 ----

    def run(self):
        args = self.args

        # 新一轮开跑就撤销上次的"优雅停止"请求 —— 启动守护这个动作本身就是
        # "我要继续下"的意思。不撤的话会看到文件就立刻停, 变成永远跑不起来。
        if procs.clear_graceful_stop():
            log("已清除上次留下的优雅停止请求(重新启动意味着继续下载)")

        log("=" * 60)
        log("下载守护启动 (pid=%d, 视频目录 %s, 本次日志 %s)"
            % (os.getpid(), paths.data_root(),
               os.path.join("logs", os.path.basename(self.run_dir))))
        log("处理顺序: %s (value=%s; 想换就改 下载设置.txt 的 order 或加 --order)"
            % (orders.order_text(self.order), self.order))
        if not args.no_takeover:
            procs.kill_leftover_managers()
        write_status(["状态: 启动中", "更新时间: %s" % now_str()])

        crashed = 0
        while self.round_no < args.max_rounds:
            # 一轮里的任何意外(磁盘满、状态文件被手工改坏、接口返回怪东西)
            # 都只能算"这一轮废了", 不能让守护进程整个退出 —— 守护一退,
            # 互斥锁就被系统放掉, 本轮起来的下载进程还在后台跑, 反而变成
            # "没有锁的下载", 下次启动还会再起一个。连续三轮都出错才退出。
            try:
                code = self.one_round()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                crashed += 1
                log("!! 第 %d 轮出错(%d/3): %r" % (self.round_no, crashed, e))
                log("   跳过这一轮; 先收掉本轮起的下载进程, 免得漏在后台")
                try:
                    procs.kill_leftover_managers()
                except Exception as e2:
                    log("   清理进程时又出错(忽略): %r" % e2)
                if crashed >= 3:
                    write_status(["状态: !! 连续 %d 轮出错, 已退出, 请查看日志"
                                  % crashed,
                                  "更新时间: %s" % now_str()])
                    return 4
                if wait_hook(60, "上一轮出错, 缓一下再试"):
                    return yield_to_new_guard()
                continue
            crashed = 0
            if code is not None:
                return code
        log("达到最大轮数 %d, 退出" % args.max_rounds)
        return 1

    def one_round(self):
        """跑一轮. 返回退出码; None = 继续下一轮."""
        args = self.args
        # 上一轮是被风控打断的: 先探一下**下载通道**是不是真通了, 还拦着就继续
        # 休息, 免得白开一轮又被撞回来(还会往状态文件里乱写失败记录)
        if self.block_aid:
            ok, why = bilitools.download_path_ok(self.block_aid)
            if not ok:
                self.block_probes += 1
                if self.block_probes > MAX_BLOCK_PROBES:
                    # 探了这么多次还不行: 这多半不是"限流还没散", 而是这个视频
                    # 本身每轮都下不成(地区限制之类)。再等下去就是永久待机 ——
                    # 放弃特殊对待, 照常开一轮, 它会被记成失败, 别的视频照下。
                    log("重开前探测: aid=%s 一直不通(%s, 试了 %d 次), 不再等它,"
                        " 照常开一轮" % (self.block_aid, why, self.block_probes))
                    self.block_aid = None
                    self.block_probes = 0
                elif self.rest_budget_used_up():
                    log("重开前探测: 下载通道还是不通(%s), 而休息档位已经用完"
                        % why)
                    return self.give_up_limited(why)
                else:
                    rest = REST_STEPS[min(self.rest_index, len(REST_STEPS) - 1)]
                    self.rest_index += 1
                    log("重开前探测: 下载通道还是不通(%s), 再休息 %d 分钟(第 %d 次)"
                        % (why, rest // 60, self.block_probes))
                    if wait_hook(rest, "下载通道不通(%s)" % why):
                        return yield_to_new_guard()
                    # 等待也消耗轮数预算, 否则 --max-rounds 形同虚设
                    self.round_no += 1
                    return None
            else:
                log("重开前探测: 下载通道已通(%s), 继续" % why)
                self.block_aid = None
                self.block_probes = 0

        self.round_no += 1
        totals, lines = analyze.status_report(self.quarantine)
        pending = totals["ghost"] + totals["pending"]
        log("第 %d 轮前统计: 缺口 %d 个 (缺文件 %d, 没下过 %d, 其中永久失败 %d)"
            % (self.round_no, pending, totals["ghost"], totals["pending"],
               totals["quarantined"]))
        top = analyze.top_line(self.quarantine, 5)
        if top:
            log("缺口最大的 5 个(看总量用的, 实际先跑谁看下面的处理顺序): %s"
                % top)
        write_status(lines + ["", "状态: 第 %d 轮运行中" % self.round_no])
        self.sweep_workdirs()
        if pending == 0:
            if not args.forever:
                log("★ 全部补齐, 没有缺口了")
                write_status(lines + ["", "状态: ★ 已全部补齐 (%s)" % now_str()])
                return 0
            # --forever: 补齐了也不退出, 等着看有没有新投稿。这里必须自己
            # 判断 forever —— 以前只在轮末判断, 结果等到下一轮开头就被上面
            # 那个 return 0 拦掉了, --forever 其实只多等了一次 30 分钟。
            log("★ 已全部补齐, --forever: 每 30 分钟再看一次有没有新视频")
            write_status(lines + ["",
                                  "状态: ★ 已全部补齐, 待命中 (%s)" % now_str()])
            self.round_no -= 1          # 待命不消耗轮数预算
            if wait_hook(1800, "等待新视频"):
                return yield_to_new_guard()
            self.stall = 0
            return None

        maintenance.prepare_round(self.quarantine, do_ghost=True)
        maintenance.prescreen_charging(self.quarantine)
        round_start, result = self.run_one_round(lines)
        return self.after_round(round_start, result, pending, lines)

    def after_round(self, round_start, result, pending_before, lines):
        """一轮结束后决定下一步. 返回退出码, 或 None 表示继续下一轮."""
        if result.stop_requested:
            if procs.graceful_stop_requested():
                # 优雅停止: 在下的已经完整落盘、状态也记好了, 现在干净退出。
                # 不清掉请求文件 —— 它是用户的意图, 该由用户(或安全停止脚本)清除,
                # 免得 "停" 之后又自己跑起来。
                log("已按优雅停止请求收工: 在下的都下完了 (%s)" % result.summary())
                write_status(lines + [
                    "",
                    "状态: 已优雅停止 (%s)" % now_str(),
                    "说明: 在下的视频已完整落盘; 想继续就重新双击 启动下载守护.bat",
                ])
                procs.guard_lock.release()
                procs.manager_lock.release()
                return 0
            log("已按请求退出, 让新守护接手 (%s)" % result.summary())
            write_status(lines + ["", "状态: 已让位给新守护 (%s)" % now_str()])
            procs.guard_lock.release()
            procs.manager_lock.release()
            return 0

        if result.login_dead:
            return self.handle_login_dead(result)

        if result.lock_busy:
            self.consecutive_lock_busy += 1
            log("另一个下载管理器在跑(第 %d 次), 等 60 秒再看"
                % self.consecutive_lock_busy)
            if self.consecutive_lock_busy >= 10:
                log("!! 一直有别的下载管理器在跑, 守护退出, 请只留一个")
                return 2
            if wait_hook(60, "另一个下载管理器在跑, 等它结束"):
                return yield_to_new_guard()
            self.round_no -= 1
            return None
        self.consecutive_lock_busy = 0

        totals, lines2 = analyze.status_report(self.quarantine)
        pending_after = totals["ghost"] + totals["pending"]

        if result.killed_reason:
            return self.handle_rate_limit(round_start, result)

        code = self.handle_clean_round(round_start, result)
        if code is not None:
            return code

        if pending_after >= pending_before and result.downloads == 0:
            self.stall += 1
            log("这一轮没有进展 (第 %d 次), 缺口还是 %d 个"
                % (self.stall, pending_after))
        else:
            self.stall = 0
        if self.stall >= STALL_ROUNDS:
            log("!! 连续 %d 轮没有进展, 停下来等你看一眼. 缺口 %d 个"
                % (self.stall, pending_after))
            write_status(lines2 + ["",
                                   "状态: !! 连续 %d 轮没有进展, 需要人工看看"
                                   % self.stall])
            return 3

        wait = 30 if result.downloads else 120
        speed = ""
        if result.downloads and result.elapsed > 0:
            speed = ", 平均 %.0f 秒/个" % (result.elapsed / result.downloads)
        log("第 %d 轮结束: 用时 %s, %s, 缺口 %d -> %d%s, 等 %d 秒后继续"
            % (self.round_no, elapsed_text(result.elapsed), result.summary(),
               pending_before, pending_after, speed, wait))
        if wait_hook(wait, "轮间等待"):
            return yield_to_new_guard()

        if self.args.forever and pending_after == 0:
            # 回到循环顶部待命 —— 那里会睡 30 分钟再看, 别在这里又睡一次
            return None
        return None


# ---------------- 单实例检查 ----------------

def take_over(args):
    """确保只有一个守护. 返回退出码, 或 None 表示可以继续."""
    holder = procs.guard_lock.acquire()
    if not holder:
        return None
    if not args.force:
        who = ("pid=%s" % holder) if holder > 0 else "pid 读不出来"
        print("已经有一个下载守护在运行 (%s), 不重复启动。" % who)
        print("只想看进度可以用:  下载守护.py --status")
        print("确实要接管它, 加 --force")
        return 4
    log("--force: 请正在运行的守护 pid=%s 退出(先礼后兵)" % holder)
    procs.ask_other_guard_to_stop(holder)
    still = procs.guard_lock.acquire(wait_seconds=20)     # 等它把锁交出来
    if still:
        log("!! 旧守护(pid=%s)还在, 不强行再开一个 —— 两个守护会抢同一个账号, "
            "反而更容易被限流、还会重复下载。" % still)
        log("   请先在任务管理器里结束它(它是 python.exe), 再启动本守护。")
        return 4
    return None


def take_manager_lock(args):
    """守护全程代持下载管理器那把锁, 保证同时只有一个下载进程."""
    holder = procs.manager_lock.acquire()
    if not holder:
        return None
    if not args.force:
        who = ("pid=%s" % holder) if holder > 0 else "pid 读不出来"
        print("已经有一个下载管理器在运行 (%s), 不启动守护。" % who)
        print("同一个账号任何时候只允许一个下载进程; 先等它结束, 或加 --force 接管")
        procs.guard_lock.release()
        return 4
    log("--force: 先把正在运行的下载管理器 pid=%s 结束掉" % holder)
    ours = procs.process_is_ours(holder, (paths.manager_script(),))
    if ours is True:
        procs.kill_tree(holder, "force 接管")
    else:
        log("pid=%s 命令行核对不通过(或没有 psutil), 不结束它 —— "
            "请到任务管理器手动确认" % holder)
    holder = procs.manager_lock.acquire(wait_seconds=15)
    if holder:
        log("!! 下载管理器(pid=%s)没能结束, 不启动守护" % holder)
        procs.guard_lock.release()
        return 4
    return None


def freeze_command(args):
    """--freeze / --unfreeze / --freeze-list / --freeze-clean 的处理.

    这几个命令**不需要拿锁**、也不会启动任何下载: 它们只改一个 JSON 文件。
    所以守护正在跑的时候也能用 —— 冻结一个 UP主 不需要先停下整个下载。
    """
    from bbdown_kit import freeze

    if args.freeze_list:
        print("\n".join(freeze.all_report_lines()))
        return 0

    if args.freeze_clean:
        removed = freeze.cleanup_stale()
        if removed:
            print("清掉 %d 条失效的冻结条目: %s" % (len(removed), ", ".join(removed)))
        else:
            print("没有失效的冻结条目, 不用清。")
        return 0

    if args.freeze:
        target = freeze.match_one(args.freeze)
        if not target:
            print("认不出要冻结哪一个: %s" % args.freeze)
            print("可以给 UID(例如 123456789), 或者文件夹名"
                  "(例如 123456789_某某UP主), 或者完整相对路径。")
            print("看看现在有哪些: 下载守护.py --freeze-list")
            return 1
        ok, why = freeze.freeze(target, args.freeze_reason)
        print("%s: %s" % (why, target))
        if ok:
            print("它不会再被更新或下载; 下载状态.json 和已下载记录原样保留。")
            print("注意: 如果它正在下载, 会在当前这一轮结束后才停下来"
                  "(想立刻停就用 stop-everything.bat)。")
            print("想看被冻结的名单: 下载守护.py --freeze-list")
            print("想启用回来:       下载守护.py --unfreeze \"%s\"" % target)
        return 0 if ok else 1

    if args.unfreeze:
        target = freeze.match_one(args.unfreeze) or args.unfreeze
        # 冻结中的名单不在 scan_up_folders 的"活跃"结果里也没关系:
        # match_one 只认磁盘上真实存在的名单, 所以这里直接用它。
        ok, why = freeze.unfreeze(target)
        print("%s: %s" % (why, target))
        if ok:
            print("下一轮(或下次一键更新)就会重新检查它的投稿更新。")
        else:
            print("看看现在冻了哪些: 下载守护.py --freeze-list")
        return 0 if ok else 1

    return None        # 不是冻结相关的命令


def main():
    global ORDER_ARG

    kit_log.setup_console()
    args = build_parser().parse_args()

    settings = config.load_settings()
    paths.refresh_data_root(settings.get("data_root", ""))
    ORDER_ARG = (args.order or "").strip() or None

    # 冻结相关命令最先处理: 它们只改一个 JSON 文件, 不拿锁、不启动下载,
    # 所以守护正在跑的时候也能用。
    code = freeze_command(args)
    if code is not None:
        return code

    quarantine = state_mod.load_json(paths.quarantine_file(), {})
    if not isinstance(quarantine, dict):
        quarantine = {}
    quarantine.pop("__done__", None)

    # ---- 只读的诊断开关, 都不碰任何状态 ----
    if args.status:
        _totals, lines = analyze.status_report(quarantine)
        print("\n".join(lines))
        return 0
    if args.audit:
        print("\n".join(analyze.audit_report(quarantine)))
        return 0
    if args.probe_login:
        verdict, detail = bilitools.probe_login()
        print("登录探测: %s (%s)" % (verdict, detail))
        print("cookie 本地过期时间: %s" % (bilitools.sessdata_expiry() or "读不到"))
        return 0
    if args.probe_api:
        bilitools.probe_api()
        return 0
    if args.probe_aids:
        bilitools.probe_aids(args.probe_aids.split(","))
        return 0
    if args.clean_workdirs:
        return clean_workdirs_command(args)

    # ---- 正式守护 ----
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = os.path.join(paths.round_log_dir(), stamp)
    rotate_guard_log(stamp)
    os.makedirs(run_dir, exist_ok=True)
    prune_run_dirs()

    code = take_over(args)
    if code is not None:
        return code
    code = take_manager_lock(args)
    if code is not None:
        return code

    guard = Guard(args, settings, quarantine, run_dir)
    return guard.run()


if __name__ == "__main__":
    atexit.register(procs.guard_lock.release)
    atexit.register(procs.manager_lock.release)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("被手动中断")
        sys.exit(130)
