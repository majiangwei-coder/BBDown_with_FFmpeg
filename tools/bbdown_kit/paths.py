# -*- coding: utf-8 -*-
"""项目里所有路径的唯一来源.

以前两个程序各自拼一遍路径、各自判断"新布局还是老布局", 只要有一处改动就会
两边不一致(比如守护按 videos\\ 统计, 管理器却把新UP主建在别处)。现在全部从这里取。

目录约定
--------
    <项目根>\\tools\\          程序(BBDown.exe / ffmpeg / BBDown.data / 本包)
    <项目根>\\videos\\         下载结果与状态
        UP主下载\\<UID>_<名字>\\      下载状态.json + 视频
        合集下载\\<名字>_<编号>\\      下载状态.json + 视频
        单视频下载\\                  下载状态.json + 视频
    <项目根>\\logs\\           日志 / 守护状态 / 永久失败名录

老布局兼容: 如果 videos\\ 不存在, 就把 tools\\ 自己当数据目录, 并且
UP主文件夹允许直接躺在数据目录根下(搬家搬一半也不会漏人)。
"""

import os

# 本文件在 <项目根>\tools\bbdown_kit\paths.py
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(PROJ)

# 三个平级大类目录的名字(状态文件里、扫描名单时都用这几个字面量)
UP_NAME = "UP主下载"
COLL_NAME = "合集下载"
SINGLE_NAME = "单视频下载"

# 每个名单文件夹里唯一的真相文件
STATE_NAME = "下载状态.json"

# 老格式的名字: 只在"迁移"时读一次, 新代码不写它们
LEGACY_RECORD_NAME = "已下载.json"
LEGACY_SKIP_NAME = "跳过.json"
LEGACY_LIST_NAME = "投稿列表.txt"

DATA_ROOT = None      # 由 refresh_data_root() 决定; 测试可以直接赋值
LOG_ROOT = None


def _resolve_data_root(raw):
    """设置里的 data_root -> 绝对路径; 空设置返回 None."""
    raw = str(raw or "").strip()
    if not raw:
        return None
    return os.path.normpath(raw if os.path.isabs(raw)
                            else os.path.join(ROOT, raw))


def _too_close_to_a_home_dir(norm):
    """是不是"用户目录附近"(它本身、它的直属子目录、或它的父目录).

    单列出来是因为语气和上面那组不同:
        · Windows / Program Files / ProgramData —— 那儿永远不该放视频, 前缀一律拦
        · 用户目录                            —— 只在**设置写得太粗**的时候出现
          (手滑写成 "C:\\Users" 或 "C:\\Users\\Administrator"),
          但 C:\\Users\\我\\Videos\\BBDown 是正常项目位置, 必须放行

    所以判据是"到二级为止": 两个方向都算 ——
        C:\\Users                        (家目录的父目录)
        C:\\Users\\Administrator         (家目录本身)
        C:\\Users\\Administrator\\Videos (家目录的直属子目录)
    再深就放行。只认路径形状, 不去碰盘(排错脚本不该因为 NTFS 权限卡住)。
    """
    try:
        home = os.path.normpath(os.path.realpath(
            os.path.expanduser("~"))).lower()
    except (OSError, ValueError):
        return False
    if not home or home.endswith(os.sep):
        return False
    if norm == home:
        return True
    if norm.startswith(home + os.sep):
        tail = norm[len(home):]
    elif home.startswith(norm + os.sep):
        tail = home[len(norm):]          # 家目录的父目录
    else:
        return False
    return tail.strip(os.sep).count(os.sep) == 0


def _data_root_ok(path):
    """data_root 的安全底线: 不接受盘符根目录、系统目录和用户目录根部.

    不是防攻击(设置文件是人自己写的), 是防手滑: 写成 "C:" 或者系统目录之后,
    守护会在那儿建出 单视频下载 这类目录, 并把状态文件撒进去。
    """
    try:
        raw = str(path or "")
        if raw.startswith("\\\\?\\") or raw.startswith("\\\\.\\"):
            return False       # \\?\C:\... 这类设备路径绕过所有常规判断
        abs_path = os.path.abspath(raw)
        # realpath 会把 8.3 短名(C:\PROGRA~1)和目录联接解开, 否则
        # "C:\PROGRA~1" 这种写法能溜过下面的系统目录检查
        norm = os.path.normpath(os.path.realpath(abs_path)).lower()
    except (OSError, ValueError):
        return False
    if norm.startswith("\\\\?\\") or norm.startswith("\\\\.\\"):
        return False
    drive, tail = os.path.splitdrive(norm)
    if tail == os.sep:
        return False                       # "C:\" 这种盘符根
    if not tail and not drive.startswith("\\\\"):
        return False                       # "C:" 这种光秃秃的盘符
    for base in (os.environ.get("SystemRoot") or r"C:\Windows",
                 os.environ.get("ProgramFiles") or r"C:\Program Files",
                 os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)",
                 os.environ.get("ProgramData") or r"C:\ProgramData"):
        base = os.path.normpath(base).lower()
        if norm == base or norm.startswith(base + os.sep):
            return False
    if _too_close_to_a_home_dir(norm):
        return False
    return True


def _warn(msg):
    """打一行警告; 局部导入 logging, 免得 paths 和 logging 循环 import."""
    try:
        from .logging import log
        log(msg)
    except Exception:
        pass


def _candidate_data_root(settings_data_root=""):
    """数据目录优先级: 设置文件 > 项目根下的 videos > tools 自己(老布局).

    设置里写的路径不安全(盘符根目录/系统目录)时忽略它、退回默认,
    并由 refresh_data_root 打一行提示。
    """
    resolved = _resolve_data_root(settings_data_root)
    if resolved is not None and _data_root_ok(resolved):
        return resolved
    videos = os.path.join(ROOT, "videos")
    return videos if os.path.isdir(videos) else PROJ


def refresh_data_root(settings_data_root=""):
    """(重新)确定数据/日志目录. 返回 (DATA_ROOT, LOG_ROOT).

    设置文件里写了 data_root 时以它为准, 否则用项目根下的 videos 目录,
    和以前的行为完全一致, 只是只算这一次; 写坏的路径会被忽略。
    """
    global DATA_ROOT, LOG_ROOT
    resolved = _resolve_data_root(settings_data_root)
    if resolved is not None and not _data_root_ok(resolved):
        _warn("data_root 设置里的 %s 是盘符根目录或系统目录, 已忽略" % resolved)
    DATA_ROOT = _candidate_data_root(settings_data_root)
    LOG_ROOT = os.path.join(ROOT, "logs")
    try:
        os.makedirs(LOG_ROOT, exist_ok=True)
    except OSError:
        LOG_ROOT = PROJ
    return DATA_ROOT, LOG_ROOT


def set_data_root(path):
    """直接指定数据目录(测试用, 也用于 apply_data_root)."""
    global DATA_ROOT
    DATA_ROOT = os.path.normpath(path)
    return DATA_ROOT


def data_root():
    if DATA_ROOT is None:
        refresh_data_root()
    return DATA_ROOT


def log_root():
    if LOG_ROOT is None:
        refresh_data_root()
    return LOG_ROOT


# ---------------- 常用具体路径(每次现算, 不缓存) ----------------

def in_data(*parts):
    """数据目录下的路径, 例如 in_data("UP主下载", "123_某某")."""
    return os.path.join(data_root(), *parts)


def up_dir():
    return in_data(UP_NAME)


def collection_dir():
    return in_data(COLL_NAME)


def single_dir():
    return in_data(SINGLE_NAME)


def state_file(folder=None):
    """某个名单文件夹的状态文件; 不传文件夹就是数据根(老布局的单视频)."""
    return os.path.join(folder or data_root(), STATE_NAME)


def settings_file():
    return os.path.join(PROJ, "下载设置.txt")


def cookie_file():
    return os.path.join(PROJ, "BBDown.data")


def bbdown_exe():
    return os.path.join(PROJ, "BBDown.exe")


def ffmpeg_exe():
    return os.path.join(PROJ, "ffmpeg-8.0-full_build", "bin", "ffmpeg.exe")


def manager_script():
    """下载管理器入口(守护要把它当子进程起)."""
    return os.path.join(PROJ, "BBDown-manager.py")


def guard_script():
    """守护的 ASCII 入口(批处理实际启动的就是它)."""
    return os.path.join(PROJ, "guard.py")


def guard_script_cn():
    """守护的真身(中文名, 直接用它启动时命令行里出现的是它)."""
    return os.path.join(PROJ, "下载守护.py")


# ---------------- 运行期文件(锁 / 谁在跑 / 停止请求) ----------------

def manager_lock():
    return os.path.join(PROJ, "下载管理器.lock")


def guard_lock():
    return os.path.join(PROJ, "守护.lock")


def manager_who():
    return os.path.join(PROJ, "下载管理器-谁在跑.txt")


def guard_who():
    return os.path.join(PROJ, "守护-谁在跑.txt")


def stop_request():
    return os.path.join(PROJ, "守护-停止请求.txt")


def graceful_stop_request():
    """"把手上这几个下完就收工"的请求文件(和上面的"立刻让位"分开).

    两个文件语义不同, 千万别合并:
        守护-停止请求.txt  立刻收摊(接管用), 里面写着目标 pid
        优雅停止请求.txt   不再开新任务, 但**在下的必须下完**; 内容为空,
                           谁看到都算(不针对某个 pid)
    """
    return os.path.join(PROJ, "优雅停止请求.txt")


# ---------------- 日志与报告 ----------------

def guard_log():
    return os.path.join(log_root(), "守护日志.txt")


def guard_status():
    return os.path.join(log_root(), "守护状态.txt")


def round_log_dir():
    return os.path.join(log_root(), "守护日志")


def quarantine_file():
    return os.path.join(log_root(), "守护-永久失败.json")


def frozen_file():
    """冻结名单(暂停某些 UP主/合集 的更新与下载).

    放 logs\\ 而不是 videos\\: 它描述的是"我要不要管它", 不是名单本身;
    而且这样挪动/改名 videos 目录也不会把冻结记录弄丢。
    """
    return os.path.join(log_root(), "冻结名单.json")
