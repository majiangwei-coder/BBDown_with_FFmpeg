# -*- coding: utf-8 -*-
"""文件锁与进程管理.

单实例互斥为什么用"操作系统的文件锁"而不是"pid 文件 + 猜那个进程活没活":
  · 两个进程同时启动时, pid 文件的方案里双方可能都先看到"没锁"于是都启动(竞态);
  · pid 会被系统复用, 残留锁的判断天然不可靠。
文件锁由操作系统维护: 谁先锁上谁独占, 进程一退出(哪怕崩溃/被强杀)锁自动消失,
所以既没有残留锁, 也没有竞态。锁文件里顺手写下的 pid 只是给人看的。

"谁在跑"单独写一个不加锁的小文件: 被字节范围锁住的文件别的句柄读不了,
所以持有者信息不能写在锁文件里。
"""

import os
import subprocess
import sys
import time

from . import paths
from .logging import log
from .util import now_str

try:
    import msvcrt                      # Windows 的文件锁
except ImportError:
    msvcrt = None

LOCK_BYTES = 1

# 拿不到锁、又不知道是谁占着时的哨兵值(必须是真值, 见 HeldLock.acquire)
UNKNOWN_HOLDER = -1


class FileLock(object):
    """操作系统级的独占文件锁."""

    def __init__(self, path, who_path=None):
        self.path = path
        self.who_path = who_path or (path + ".who")
        self.fd = None
        self.holder = None      # 现在(或抢占失败时)占着它的 pid

    def read_holder(self):
        """"谁在跑"文件里的 pid; 读不出来或不像 pid 就返回 None.

        这个文件只是张"名片", 谁都能写, 所以只当线索用 —— 真要结束进程之前
        还有一道命令行身份校验(见 process_is_ours)。
        """
        try:
            with open(self.who_path, "r", encoding="utf-8", errors="ignore") as f:
                text = (f.read() or "").strip().split()
            pid = int(text[0]) if text else None
        except Exception:
            return None
        if pid is None or not 0 < pid < 2 ** 31:
            return None
        return pid

    def _write_who(self):
        """写"谁在跑"名片. 先写临时文件再改名, 免得留下半截 pid.

        半截名片读出来是"pid 不可信", 调用方会当成"有人占着但说不清", 白让
        用户去任务管理器手工确认。临时文件名带自己的 pid: 两个进程同时写
        名片时不会互相覆盖。
        """
        tmp = "%s.%d.tmp" % (self.who_path, os.getpid())
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("%d %s\n" % (os.getpid(), now_str()))
            os.replace(tmp, self.who_path)
        except OSError:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass

    def _clear_who(self):
        try:
            if self.read_holder() != os.getpid():
                return              # 不是我的, 别动
            os.remove(self.who_path)
        except Exception:
            pass

    def acquire(self):
        """拿到返回 True; 被别人占着返回 False(对方 pid 在 self.holder)."""
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR)
        except OSError:
            self.holder = self.read_holder()
            return False
        if msvcrt is not None:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, LOCK_BYTES)
            except OSError:
                os.close(fd)
                self.holder = self.read_holder()
                return False
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, ("%d" % os.getpid()).encode("utf-8"))
        except OSError:
            pass
        self.fd = fd
        self.holder = os.getpid()
        self._write_who()
        return True

    def release(self):
        if self.fd is None:
            return
        try:
            if msvcrt is not None:
                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, LOCK_BYTES)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        self.fd = None
        self._clear_who()


# ---------------- 谁在跑 / 进程信息 ----------------

def _pid_image(pid):
    """pid 现在是什么程序. 返回 (是否活着, 镜像名小写).

    问不出来时返回 (None, "") —— 调用方按"说不清, 保守处理"来办。
    这是不用 psutil 的那条路(Win7+ 都自带 tasklist), 免得依赖没装上的库。
    """
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "PID eq %d" % int(pid), "/FO", "CSV", "/NH"],
            capture_output=True, text=True, errors="replace", timeout=20,
        )
    except Exception:
        try:                 # tasklist 都起不来(极少): 退到 OpenProcess
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True, ""
            return False, ""
        except Exception:
            return None, ""
    return _parse_tasklist(out.stdout or "", int(pid))


def _parse_tasklist(text, pid):
    """tasklist /FO CSV 的输出 -> (是否活着, 镜像名小写).

    老老实实按 CSV 解析: 以前用"pid 是不是出现在输出里"加逗号切分, 镜像名
    里带逗号、或提示行里出现同样的数字都会认错, 而镜像名正是"要不要杀"的
    依据。中文系统上的"没有运行的任务..."提示行解析不出列, 自然被跳过。
    """
    import csv
    import io

    try:
        rows = list(csv.reader(io.StringIO(text)))
    except Exception:
        return None, ""
    for row in rows:
        if len(row) < 2:
            continue
        try:
            if int(row[1].strip()) == pid:
                return True, row[0].strip().lower()
        except (TypeError, ValueError):
            continue
    return False, ""


def image_name(pid):
    """pid 的镜像名(小写, 如 python.exe). 取不到返回空串."""
    return _pid_image(pid)[1]


def pid_alive(pid):
    """这个 pid 还在不在(不关心是谁). 不依赖 psutil."""
    alive = _pid_image(pid)[0]
    return True if alive is None else alive


def lock_holder(path, who_path=None):
    """这把锁现在被谁占着? 没人占返回 None.

    只探一下就放, 全程不写: 不建锁文件、不动"谁在跑"名片。以前是"真去
    acquire 一把, 拿到再 release" —— 查询一次就顺手建了文件、还把锁文件里
    别人的 pid 覆盖成自己的, --status 这种只读命令凭空留下写副作用。
    """
    if not os.path.exists(path):
        return None                 # 锁文件都没有 -> 没人占(名片可能是残留)
    try:
        fd = os.open(path, os.O_RDWR)          # 不带 O_CREAT: 没有也绝不建
    except OSError:
        return FileLock(path, who_path).read_holder()
    try:
        if msvcrt is None:
            return None
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, LOCK_BYTES)
        except OSError:
            return FileLock(path, who_path).read_holder()   # 被占着 -> 看名片
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, LOCK_BYTES)
        return None
    except OSError:
        return FileLock(path, who_path).read_holder()
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def running_now_text():
    """一句话回答"现在有在跑的吗": 两把锁分别被谁占着."""
    bits = []
    for label, path, who in (("下载守护", paths.guard_lock(), paths.guard_who()),
                             ("下载管理器", paths.manager_lock(),
                              paths.manager_who())):
        pid = lock_holder(path, who)
        if pid:
            bits.append("%s pid=%s(%s)" % (label, pid, image_name(pid) or "?"))
    if not bits:
        return "没有(守护和下载管理器都没在跑)"
    return "; ".join(bits)


# ---------------- 结束进程 ----------------

def _psutil():
    try:
        import psutil
    except ImportError:
        return None
    return psutil


def _project_exes():
    """本项目自己的可执行文件(小写全路径): BBDown.exe 和 ffmpeg.exe."""
    return {os.path.normcase(os.path.abspath(p))
            for p in (paths.bbdown_exe(), paths.ffmpeg_exe())}


def is_project_process(proc):
    """这个进程是不是本项目的那两个 exe(按可执行文件路径认).

    以前收尾按"进程名叫 bbdown.exe/ffmpeg.exe"全系统杀, 会把用户在别处
    手动开的同名进程一起杀掉; 现在只认本项目目录下的那两个。
    """
    try:
        exe = os.path.normcase(os.path.abspath(proc.exe() or ""))
    except Exception:
        return False
    return exe in _project_exes()


def process_is_ours(pid, markers):
    """pid 的命令行里是否含本项目标记.

    返回 True 确认是本项目的进程; False 确认不是; None 说不清(没有 psutil
    或读不到命令行) —— 调用方对"不是 True"一律不杀。

    锁文件和"谁在跑"文件都是普通文本, 谁都能写, 拿里面的 pid 直接 kill
    等于给了别人一个"杀任意 python 进程"的入口, 所以动手前必须核对命令行。
    """
    psutil = _psutil()
    if not psutil:
        return None
    try:
        cmd = " ".join(psutil.Process(pid).cmdline() or [])
    except Exception:
        return None
    lowered = cmd.lower()
    for marker in markers:
        if marker.lower() in lowered:
            return True
    return False


def kill_tree(pid, why, verify=None):
    """结束一个进程和它的所有子进程, 连漏网的 BBDown/ffmpeg 一起收掉.

    verify(pid) -> True 才算身份核对通过. 核对两次: 进来一次、真正 kill 之前
    再一次 —— pid 是从文本文件里读出来的, 系统随时可能把它回收给一个新进程,
    只核对一次的话, 中间那一瞬就是"杀错人"。
    """
    killed = []
    psutil = _psutil()
    if psutil:
        try:
            proc = psutil.Process(pid)
            if verify is not None and not verify(pid):
                log("pid=%s 身份核对不通过, 不结束它" % pid)
                return
            procs = proc.children(recursive=True)
            for child in procs:
                try:
                    child.kill()
                    killed.append(child.name())
                except Exception:
                    pass
            if verify is not None and not verify(pid):
                log("pid=%s 动手前身份变了, 只结束它的子进程" % pid)
                psutil.wait_procs(procs, timeout=10)
                return
            proc.kill()
            killed.append(proc.name())
            psutil.wait_procs(procs + [proc], timeout=10)
        except Exception:
            pass
    else:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True)
    time.sleep(1.0)
    if psutil:
        # 收尾: 上面被杀的 BBDown 可能还没退出, 或已经变成孤儿.
        # 只认本项目那两个 exe 的路径 —— 别把用户在别处开的同名进程也杀掉。
        for proc in psutil.process_iter(["name"]):
            try:
                if not is_project_process(proc):
                    continue
                proc.kill()
                killed.append(proc.info.get("name") or "?")
            except Exception:
                pass
    log("已结束进程树(%s): %s" % (why, ", ".join(sorted(set(killed))) or "无"))


def kill_leftover_managers():
    """接管前清理: 结束还在跑的、属于本项目的下载管理器/BBDown.

    只动自己的东西: 管理器要"镜像名是 python 且命令行里含本项目管理器脚本的
    绝对路径", BBDown/ffmpeg 要"可执行文件就是本项目目录下的那两个"。以前是
    按进程名全系统扫, 打开本文件的编辑器、别处手开的 BBDown 都会被误伤。

    锁文件不在这里删: 本函数被调用时守护自己正持有它, 删掉只会破坏互斥
    (Windows 上虽然多半因共享冲突删不掉, 但那是运气, 不是设计)。
    """
    victims = []
    psutil = _psutil()
    if psutil:
        marker = paths.manager_script()
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                name = (proc.info.get("name") or "").lower()
                cmd = " ".join(proc.info.get("cmdline") or [])
                if name.startswith("python") and marker.lower() in cmd.lower():
                    victims.append(proc.info["pid"])
            except Exception:
                pass
        for proc in psutil.process_iter(["name"]):
            try:
                if is_project_process(proc):
                    victims.append(proc.pid)
            except Exception:
                pass
    for pid in victims:
        kill_tree(pid, "接管")
    if not victims:
        log("没有发现正在运行的下载管理器")


def _guard_marker_hit(lowered_cmd):
    """命令行里是不是本项目那个守护(按绝对路径认)."""
    for p in (paths.guard_script(), paths.guard_script_cn()):
        if os.path.normcase(p).lower() in lowered_cmd:
            return True
    return False


def other_guard_pids():
    """除了自己以外, 还在跑的本项目守护进程(给 --force 用)."""
    found = []
    psutil = _psutil()
    if not psutil:
        return found
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            if proc.info["pid"] == os.getpid():
                continue
            cmd = " ".join(proc.info.get("cmdline") or []).lower()
            # 绝对路径: 以前用 "下载守护.py" 这个相对名, 一是会认错别处的同名
            # 脚本, 二是批处理实际启动的是 tools\guard.py, 根本匹配不上
            if _guard_marker_hit(cmd):
                found.append(proc.info["pid"])
        except Exception:
            pass
    return found


# ---------------- 守护 / 管理器各自的那把锁 ----------------

class HeldLock(object):
    """一把具名锁的持有者(整个进程生命周期都占着, 退出时放掉)."""

    def __init__(self, path, who_path):
        self.lock = FileLock(path, who_path)

    def acquire(self, wait_seconds=0):
        """占住. 返回 None = 拿到了; 返回真值 = 被别人占着.

        拿不到锁但"谁在跑"文件不可信时返回 UNKNOWN_HOLDER —— 必须是真值:
        调用方都用 `if not holder:` 判断"锁是不是空的", 返回 0/None 会被当成
        没人占, 于是第二个实例就放行进来了。
        """
        deadline = time.time() + max(0, wait_seconds)
        while True:
            if self.lock.acquire():
                return None
            if time.time() >= deadline:
                return self.lock.holder or UNKNOWN_HOLDER
            time.sleep(1)

    def release(self):
        self.lock.release()


guard_lock = HeldLock(paths.guard_lock(), paths.guard_who())
manager_lock = HeldLock(paths.manager_lock(), paths.manager_who())


# "这是我们项目的进程"的判据: 一律用绝对路径. 用 "下载守护.py" 这种相对名
# 判定, 等于给别处一个同名脚本开了后门 —— 而批处理实际启动的是 tools\guard.py。
_OUR_MARKERS = (paths.manager_script(), paths.guard_script(),
                paths.guard_script_cn())


def stop_requested_for_me():
    """另一个守护(或用户)请我退出吗? 停止请求文件里写着我的 pid 就是要我走."""
    try:
        with open(paths.stop_request(), "r", encoding="utf-8") as f:
            return (f.read() or "").strip() == str(os.getpid())
    except OSError:
        return False


def graceful_stop_requested():
    """有人要求"优雅停止"吗? 文件存在就算(内容不管, 不针对某个 pid).

    优雅停止 = 不再开新任务, 但在下的这几个下完、完整落盘之后才退出。
    所以这个判定只该用在"要不要取下一个任务"的地方, 绝不能拿来杀进程。
    """
    return os.path.exists(paths.graceful_stop_request())


def ask_graceful_stop():
    """写一个优雅停止请求(空文件). 返回是否写成功."""
    try:
        with open(paths.graceful_stop_request(), "w", encoding="utf-8") as f:
            f.write("stop\n")
        return True
    except OSError as e:
        log("写优雅停止请求失败: %r" % e)
        return False


def clear_graceful_stop():
    """撤销优雅停止请求(启动新一轮之前调用). 返回是否删掉了."""
    try:
        os.remove(paths.graceful_stop_request())
        return True
    except OSError:
        return False


def ask_other_guard_to_stop(pid, wait_seconds=25):
    """请另一个守护退出, 返回它是否已经不在了.

    先留"停止请求"(新版本守护每秒会看一眼, 看到就自己收摊), 等一会儿;
    老版本不认请求、命令行又核对得上, 才退回到杀进程。

    pid 是"谁在跑"文件里的数字, 那是普通文本谁都能写, 所以动手前必须核对
    命令行; 核对不过(或没有 psutil 说不清)就只报告、不杀 —— 杀错了是别人
    的进程, 杀不了最坏也只是让用户手动去任务管理器结束。这条路径上的降级
    是"不杀", 调用方会在 20 秒后返回退出码 4, 不会出现两个守护同时下。
    """
    if pid <= 0:
        log("锁被占着但 pid 不可信, 请到任务管理器手动结束那个 python 进程")
        return
    try:
        with open(paths.stop_request(), "w", encoding="utf-8") as f:
            f.write(str(pid))
    except OSError:
        pass
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if not pid_alive(pid):
            break
        time.sleep(1)
    try:
        # 只删自己写的那张请求: 这中间可能有人又写了一张新的(比如用户想停
        # 另一个守护), 一律删掉会把它带走
        with open(paths.stop_request(), "r", encoding="utf-8") as f:
            still_mine = (f.read() or "").strip() == str(pid)
        if still_mine:
            os.remove(paths.stop_request())
    except OSError:
        pass
    if not pid_alive(pid):
        return
    if not image_name(pid).startswith("python"):
        log("锁里的 pid=%s 现在不是 python 进程, 当作残留锁处理" % pid)
        return
    ours = process_is_ours(pid, _OUR_MARKERS)
    if ours is not True:
        log("pid=%s 命令行核对不通过, 不结束它 —— 请到任务管理器手动确认" % pid)
        return
    log("旧守护(pid=%s)没理停止请求, 直接结束它" % pid)
    kill_tree(pid, "force 接管",
              verify=lambda p: process_is_ours(p, _OUR_MARKERS) is True)
    time.sleep(2)


def self_check():
    """给测试/自检用: 当前进程能不能用文件锁."""
    if msvcrt is None:
        return "不支持(非 Windows)"
    return "ok pid=%d" % os.getpid()


if __name__ == "__main__":              # 手动排查: python -m bbdown_kit.procs
    sys.stdout.write("python=%s\n" % sys.executable)
    sys.stdout.write("锁: %s\n" % self_check())
    sys.stdout.write("现在在跑的: %s\n" % running_now_text())
