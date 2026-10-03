# -*- coding: utf-8 -*-
"""安全停止: 收掉所有正在下载的本项目进程, 清掉本次留下的残骸, 并把现状报清楚.

双击 安全停止.bat 会用这个。设计上只管三件事, 而且顺序不能换:

    1. 停  —— 从根开始收掉整棵进程树: 守护 -> 管理器 -> BBDown -> ffmpeg
    2. 验  —— 反复确认"一个都不剩"; **只要还剩一个进程, 就一个文件都不删**
    3. 清  —— 确认干净之后才动手: 残留的 <aid> 临时目录、写坏的 .tmp、
              幽灵记录(有记录没文件)

为什么必须"先确认没有进程再删文件": 那些 <aid> 临时目录里就是正在下载的分片。
只要还有一个 BBDown 活着, 它可能正在写这个目录 —— 这时候删就是把它从底下抽走。
确认干净之后再删, 才谈得上安全。

为什么可以杀掉就立刻删(不等 10 分钟): workdirs 平时的 10 分钟冷静期是为了防止
"删掉正在下载的目录"。而这里我们先做了第 1、2 步, 进程已经确认全死 ——
那个担心不存在了, 所以这一步可以放开 stale 检查。规则(目录名是 aid、
里面全是该 aid 的临时产物、没有子目录)依然照旧, 一条都不放宽。

重复运行是安全的: 没有进程就跳过第 1 步, 没有残骸就跳过第 3 步。
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bbdown_kit import media, paths, procs, state as state_mod, tasks, workdirs
from bbdown_kit import logging as kit_log
from bbdown_kit.util import size_text

log = kit_log.log

# 等进程自己退干净最多等多久(秒). taskkill /F 通常几秒内就完事。
WAIT_SECONDS = 30

# 优雅停止最多等多久(分钟). 在下的视频本来就该在"单视频超时上限"内下完
# (默认 30 分钟), 所以 40 分钟足够; 真卡住了由超时兜底转强杀。
DEFAULT_WAIT_MINUTES = 40


def _psutil():
    try:
        import psutil
    except ImportError:
        return None
    return psutil


def project_script_paths(project_dir):
    """project_dir 这一份项目里"可能出现在命令行上"的文件(全部小写、正斜杠).

    脚本都在 tools\\ 下; 但 BBDown.exe 和 ffmpeg.exe 的位置以 paths 定义的
    为准(实测 ffmpeg 在 tools\\ffmpeg-8.0-full_build\\bin\\ 里, 不在 tools\\
    根 —— 这里写错层级就等于认不出该杀的 ffmpeg)。
    """
    base = str(project_dir).replace("\\", "/").lower().rstrip("/")
    names = ["下载守护.py", "guard.py", "BBDown-manager.py",
             "BBDown.exe", "ffmpeg.exe",
             "ffmpeg-8.0-full_build/bin/ffmpeg.exe",
             "ffmpeg-8.0-full_build/bin/ffprobe.exe"]
    out = []
    for name in names:
        path = "%s/%s" % (base, name.lower())
        if path not in out:
            out.append(path)
    # 兜底: 这两个 exe 的**权威位置**由 paths 说了算(实测 ffmpeg 在
    # tools\ffmpeg-8.0-full_build\bin\ 下, 不在 tools\ 根 —— 手写层级
    # 很容易写错, 写错就等于认不出该杀的 ffmpeg)。
    for real in (paths.bbdown_exe(), paths.ffmpeg_exe()):
        path = str(real).replace("\\", "/").lower()
        if path not in out:
            out.append(path)
    return out


def matches_project(project_dir, cmdline):
    """命令行里有没有 **project_dir 这一份** 项目的标记. 拿不准返回 False(不杀).

    纯函数(项目目录当参数传进来), 方便直接测。

    只认带完整路径的那几个文件。**绝不能**用 "bbdown-manager.py" 这种不带
    路径的片段 —— 那样会把"另一个项目副本"(沙盒、备份目录、第二份安装)里的
    进程一起杀掉。踩过这个坑: 测试脚本自己起了沙盒进程, 结果把真实项目正在
    跑的管理器给收了。

    代价是: 别的副本不会被这个脚本停掉。这是有意的 —— 宁可按不动, 也不误伤;
    而且两把锁本来就保证同时只有一个副本在下载。
    """
    if not cmdline:
        return False
    text = " ".join(cmdline).replace("\\", "/").lower()
    return any(path in text for path in project_script_paths(project_dir))


def _matches_project(cmdline):
    """生产用的包装: 认"当前这一份"项目."""
    return matches_project(paths.PROJ, cmdline)


def find_project_processes():
    """本项目自己起的那些进程: [(pid, 名字, 命令行), ...].

    靠 psutil 读命令行来认 —— 不靠进程名, 否则会误伤别处同名的 python/ffmpeg。
    读不到命令行(没 psutil / 权限不足)时返回空, 调用方按"说不清"处理。
    """
    psutil = _psutil()
    if not psutil:
        return []
    found = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.info["pid"] == os.getpid():
                continue
            if _matches_project(proc.info.get("cmdline")):
                found.append((proc.info["pid"], proc.info.get("name") or "",
                              " ".join(proc.info.get("cmdline") or [])))
        except Exception:
            continue
    return found


def _kill_pid(pid, why):
    """收掉一个进程和它的子进程. 用 taskkill /T /F(不依赖 psutil)."""
    import subprocess

    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=30)
        return True
    except Exception as e:
        log("taskkill pid=%s 失败: %r" % (pid, e))
        return False


def stop_everything(wait_seconds=WAIT_SECONDS):
    """第 1、2 步: 收掉所有本项目进程, 并确认真的收干净了.

    返回剩下的进程列表; 空列表 = 确认干净, 可以进入第 3 步。
    """
    victims = find_project_processes()
    if not victims:
        log("没有发现正在运行的本项目进程(守护/管理器/BBDown/ffmpeg 都没有)")
        return []

    log("发现 %d 个进程, 开始收:" % len(victims))
    for pid, name, cmd in victims:
        log("   pid=%-7s %-14s %s" % (pid, name, cmd[:96]))

    # 从根往下收: 先收守护和管理器, 再收 BBDown/ffmpeg。
    # 反过来先收子进程的话, 父进程(管理器)会立刻再起一个新的。
    def rank(cmd):
        if "guard.py" in cmd or "下载守护.py" in cmd:
            return 0
        if "bbdown-manager.py" in cmd.lower():
            return 1
        return 2

    for pid, name, cmd in sorted(victims, key=lambda v: rank(v[2])):
        log("收掉 pid=%s (%s)" % (pid, name))
        _kill_pid(pid, "安全停止")

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        left = find_project_processes()
        if not left:
            log("✔ 已确认: 本项目进程一个都不剩")
            return []
        time.sleep(1)

    left = find_project_processes()
    log("!! 等了 %d 秒还有 %d 个进程没退掉:" % (wait_seconds, len(left)))
    for pid, name, cmd in left:
        log("   pid=%-7s %-14s %s" % (pid, name, cmd[:96]))
    log("   它们可能权限更高(比如以管理员身份启动的)。请用管理员身份重跑本脚本,")
    log("   或在任务管理器里结束它们之后, 再跑一次。")
    return left


def _scan_workdirs_any_age():
    """扫全部名单文件夹里的 <aid> 残留目录, **不看**它多久没动过.

    平时 workdirs 会跳过"10 分钟内还在动"的目录, 免得删掉正在下载的东西。
    但在本脚本里第 1、2 步已经确认所有进程都死了 —— 那个担心不存在了,
    所以这里放开冷静期, 好把"刚被我们杀掉的"那个目录也认出来。

    其余规则一条不动: 目录名必须是纯数字 aid(>=6 位)、里面不能有子目录、
    每个文件都必须属于这个 aid 且在临时产物清单里(workdirs.cleanable 判定)。
    """
    found = []
    for name in tasks.scan_all_folders():
        folder = os.path.join(paths.data_root(), name)
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            if not (entry.name.isdigit() and len(entry.name) >= workdirs.MIN_AID_LEN):
                continue
            ok, _why = workdirs.cleanable(entry.path, entry.name,
                                          now=time.time(), stale_seconds=0)
            if ok:
                found.append(workdirs.Found(entry.path, entry.name,
                                            media.folder_bytes(entry.path)))
    return found


def clean_leftovers():
    """第 3 步(只在确认无进程后调用): 清临时工作目录、坏 .tmp、幽灵记录."""
    report = {"workdirs": 0, "freed": 0, "tmp": 0, "ghosts": 0, "folders": 0}

    # 3.1 残留的 <aid> 临时目录. 进程已确认全死, 所以放开 10 分钟冷静期,
    # 否则"刚刚被我们杀掉"的那个目录反而会被跳过(它正是刚动过的)。
    found = _scan_workdirs_any_age()
    cleaned = 0
    freed = 0
    for item in found:
        got = workdirs.remove_after_kill(os.path.dirname(item.path), item.name)
        if got:
            cleaned += 1
            freed += got
        elif not os.path.exists(item.path):
            cleaned += 1          # 空目录已经没了, 也算清掉
    report["workdirs"] = cleaned
    report["freed"] = freed
    if cleaned:
        log("清掉 %d 个下载残留的临时目录 (释放 %s)"
            % (cleaned, size_text(freed)))
    elif found:
        log("有 %d 个临时目录没清掉(可能还被占着), 下次启动守护时会再扫一遍"
            % len(found))
    else:
        log("没有下载残留的临时目录")

    # 3.2 上次异常退出留下的 .tmp(状态文件的半成品)
    for name in tasks.scan_all_folders():
        folder = os.path.join(paths.data_root(), name)
        tmp = paths.state_file(folder) + ".tmp"
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
                report["tmp"] += 1
            except OSError as e:
                log("删不掉 %s: %r" % (tmp, e))
    if report["tmp"]:
        log("清掉 %d 个半成品 .tmp" % report["tmp"])

    # 3.3 幽灵记录(有『已下载』但本地没文件). 这一步只动状态文件, 是幂等的:
    # 没有幽灵就什么都不写。
    for name in tasks.scan_all_folders():
        folder = os.path.join(paths.data_root(), name)
        st = state_mod.State.load(folder)
        index = media.MediaIndex(folder)
        dup = st.duplicate_titles()
        ghosts = []
        for aid in st.record:
            title = st.title_of(aid)
            if title is None:
                continue        # 不在名单里了(UP主删了), 记录留着不动
            if not index.has(aid, title, dup):
                ghosts.append(aid)
        if ghosts:
            for aid in ghosts:
                st.record.pop(aid, None)
            if st.save():
                report["ghosts"] += len(ghosts)
                report["folders"] += 1
            else:
                log("!! 幽灵记录写回失败: %s" % name)
    if report["ghosts"]:
        log("撤回 %d 条幽灵记录(有记录没文件), 它们回到待下载"
            % report["ghosts"])

    return report


def summary():
    """收尾报账: 现在缺多少、有没有东西在跑."""
    from bbdown_kit import analyze

    quarantined = state_mod.load_json(paths.quarantine_file(), {})
    if not isinstance(quarantined, dict):
        quarantined = {}
    totals, _lines = analyze.status_report(quarantined)
    log("现在在跑: %s" % procs.running_now_text())
    log("名单 %d 条, 已记录 %d 条, 缺文件 %d 条, 待补齐 %d 个"
        % (totals["total"], totals["recorded"], totals["ghost"],
           totals["ghost"] + totals["pending"]))


def wait_until_stopped(timeout_seconds, poll_seconds=5):
    """等进程自己退干净. 返回还在跑的进程列表(空 = 干净了).

    优雅停止期间心跳: 每 30 秒报一次"还在下几个", 让人知道它确实在收尾
    而不是卡住了。等待期间**不碰任何文件**。
    """
    deadline = time.time() + timeout_seconds
    last_note = 0.0
    while True:
        left = find_project_processes()
        if not left:
            return []
        now = time.time()
        if now >= deadline:
            return left
        if now - last_note >= 30:
            last_note = now
            log("还在收尾: 剩 %d 个进程, 最多再等 %s"
                % (len(left), _elapsed_text(deadline - now)))
        time.sleep(poll_seconds)


def _elapsed_text(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "%d 秒" % seconds
    return "%d 分 %02d 秒" % (seconds // 60, seconds % 60)


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--yes", action="store_true",
                    help="不询问, 直接开始优雅停止(给自动化用)")
    ap.add_argument("--dry-run", action="store_true",
                    help="只看会做什么, 什么都不做")
    ap.add_argument("--force", action="store_true",
                    help="不等它下完, 立刻强杀整棵进程树(然后清残留)")
    ap.add_argument("--timeout", type=int, default=DEFAULT_WAIT_MINUTES,
                    metavar="分钟",
                    help="优雅停止最多等多久(默认 %d 分钟), 超时才强杀"
                         % DEFAULT_WAIT_MINUTES)
    ap.add_argument("--keep-workdirs", action="store_true",
                    help="强杀时保留残留的临时目录(默认会清掉)")
    args = ap.parse_args()

    kit_log.setup_console()

    victims = find_project_processes()

    if args.dry_run:
        print("正在运行的进程: %d 个" % len(victims))
        for pid, name, cmd in victims:
            print("   pid=%-7s %-14s %s" % (pid, name, cmd[:96]))
        if not victims:
            print("   (没有在跑的)")
        if victims and not args.force:
            print()
            print("默认做法(优雅停止): 请求它们不再开新任务, 等在下的下完再退。")
            print("  这样不会留下半成品, 也就不需要事后清理。")
        elif args.force:
            print()
            print("--force: 会立刻强杀, 然后清理残留的临时目录。")
        return 0

    if not victims:
        log("=" * 60)
        log("没有发现正在运行的本项目进程 —— 已经停着了")
        found = _scan_workdirs_any_age()
        if found:
            # 这种情况说明上次是被掐断的(或者手工删过进程), 顺手清掉
            log("不过发现 %d 个下载残留的临时目录(共 %s)"
                % (len(found), size_text(sum(f.bytes for f in found))))
            if args.keep_workdirs:
                log("按 --keep-workdirs 的要求保留, 不动它们")
            else:
                clean_leftovers()
        summary()
        return 0

    if not args.yes:
        if args.force:
            print("即将【立刻强杀】%d 个进程(会留下半成品, 之后清理):"
                  % len(victims))
        else:
            print("即将【优雅停止】%d 个进程:" % len(victims))
        for pid, name, cmd in victims:
            print("   pid=%-7s %-14s %s" % (pid, name, cmd[:90]))
        print()
        if not args.force:
            print("做法: 让它们不再开新任务, 把手上正在下的下完、完整落盘之后再退。")
            print("     最多等 %d 分钟, 超时才会强杀。中途可以按 Ctrl+C 取消。"
                  % args.timeout)
        print()
        try:
            answer = input("确定吗? (Y/N): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
        if answer not in ("y", "yes", ""):
            print("已取消, 什么都没动")
            return 1

    log("=" * 60)

    if args.force:
        return _force_stop(args)

    # ---------- 主路径: 优雅停止 ----------
    log("优雅停止: 请它们不再开新任务, 在下的下完再退")
    if not procs.ask_graceful_stop():
        log("!! 写优雅停止请求失败, 改用强杀")
        return _force_stop(args)
    log("已发出请求(优雅停止请求.txt)。等它们把手上的下完 ...")

    left = wait_until_stopped(args.timeout * 60)
    if left:
        log("!! 等了 %d 分钟还没退完, 剩 %d 个:" % (args.timeout, len(left)))
        for pid, name, cmd in left:
            log("   pid=%-7s %-14s %s" % (pid, name, cmd[:90]))
        log("   退回强杀(这次会留下半成品, 下面会清掉)")
        procs.clear_graceful_stop()
        return _force_stop(args)

    procs.clear_graceful_stop()
    log("✔ 优雅停止完成: 没有残留进程")
    found = _scan_workdirs_any_age()
    if found:
        # 正常不该发生; 真出现了说明有下载是被掐断的, 顺手清掉
        log("意外: 还发现 %d 个临时目录(共 %s), 说明有下载没下完, 现在清掉"
            % (len(found), size_text(sum(f.bytes for f in found))))
        clean_leftovers()
    else:
        log("✔ 没有残留的临时目录(在下的都完整落盘了)")
    log("-" * 60)
    summary()
    log("安全停止: 完成 —— 想继续就重新双击 启动下载守护.bat")
    return 0


def _force_stop(args):
    """强杀 + 清理(超时兜底, 或用户要 --force). 返回退出码."""
    log("强杀: 收掉整棵进程树")
    left = stop_everything()
    if left:
        # 还有进程没死: 绝不删文件, 免得把正在写的分片抽走
        log("!! 还有进程没结束, 为安全起见这一步不删任何文件")
        log("   请先让上面那些进程退出, 然后再跑一次本脚本")
        return 2
    if args.keep_workdirs:
        log("按 --keep-workdirs 的要求, 保留下载残留的临时目录")
    else:
        clean_leftovers()
    log("-" * 60)
    summary()
    log("安全停止: 完成 —— 没有残留进程, 状态文件可用, 可以随时重新启动守护")
    return 0


if __name__ == "__main__":
    sys.exit(main())
