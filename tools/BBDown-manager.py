# -*- coding: utf-8 -*-
"""BBDown 下载管理器(入口).

这个文件很薄: 只负责"问用户想干什么"和"把参数解析出来", 真正干活的逻辑都在
tools\\bbdown_kit\\ 里。双击 下载管理器.bat 跑的就是它。

功能:
  - 先检查登录状态
  - 列出已有UP主文件夹(UID_UP主名), 支持 ↑/↓ 移动、空格多选、回车开始更新
  - 按 A 键一键更新全部UP主, 全程不再询问, 自动增量下载
  - 按 N 键进入"下载新UP主", 输入主页链接后自动识别 UID 和 UP主名
  - 按 S 键进入"下载单个视频", 支持 BV号 / av号 / b23.tv短链
  - 按 C 键下载合集/系列(粘贴合集链接)
  - 每个UP主一个文件夹, 所有内容都放在里面
  - 投稿列表/已下载/跳过记录统一保存在 <文件夹>/下载状态.json
  - 只下载没下载过的新视频, 已下载的自动跳过
  - 投稿列表增量校验: 每次先用 1 次请求拉最新一页, 和本地记录核对;
    没变化就直接沿用本地列表; 有新增时只往前翻到见过的视频为止再合并。
    本地保存的投稿列表始终是完整的, 所以「已下载」「跳过」「被UP主删除」
    这些判断逻辑和以前完全一致.
  - UP主删除的旧视频: 本地文件保留, 不删除
  - 正式下载前要求手动确认(一键更新/守护时自动跳过询问)
  - 下载失败的视频自动加入"跳过"记录, 以后不再重试;
    如需重试, 在菜单中选择『R 重新下载所有已跳过项目』(或加 --retry-skip)
  - 连续失败达到阈值(默认10个)时自动判定为被限流: 并发降为1、加大间隔、
    暂停一段时间后自动继续; 恢复正常后自动还原
  - 所有视频统一命名为 标题_aid(单P: 某某标题_100000000000000.mp4;
    多P: 标题_aid/[P01]xxx.mp4), 避免同标题互相覆盖

进阶参数(一般用不到):
  --url <链接>          跳过菜单, 直接处理该链接对应的UP主/合集
  --video <链接>        跳过菜单, 直接下载单个视频到 单视频下载/
  --collection <链接>   跳过菜单, 直接下载该合集/系列
  --limit <数量>         每个UP主本次最多下载多少个视频(0 表示只刷新列表不下载)
  --parallel <数量>       同时下载的视频数(默认读 下载设置.txt)
  --bbdown-args "<参数>" 透传给 BBDown 的附加参数, 例如 "--cover-only"
  --yes                 跳过下载前的确认提示
  --all                 等价于在菜单中按 A, 一键更新全部已有UP主
  --retry-skip          把「跳过」列表里的视频重新加入下载队列, 重试下载
  --backfill           把"有记录但本地无实际文件"的重复标题视频重新下载补回内容
  --full                强制完整拉取投稿列表(排查问题用, 会慢一些)
  --full-days <天数>     每隔多少天至少完整校验一次(默认读 下载设置.txt)
  --order <顺序>         这一轮先处理谁: collection_first/smallest/largest/name
                         /duration_small/duration_large(后两个按**总时长**排)
  --event-log <文件>     把进度写成一行的 JSON 事件(守护用它统计, 人也可以看)
"""

import argparse
import os
import re
import shlex
import sys
import time

# 让"直接双击 python 文件"和"被守护当子进程拉起"两种方式都能 import 本包
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bbdown_kit import (bilitools, config, download, duration, orders, paths,
                        procs, tasks, workdirs)
from bbdown_kit import logging as kit_log
from bbdown_kit.state import record_count
from bbdown_kit.logging import highlight
from bbdown_kit.util import size_text

# 兼容历史名字(老代码/老文档里出现过, 保留以免外部脚本引用不到)
STATE_NAME = paths.STATE_NAME
COLL_NAME = paths.COLL_NAME
UP_NAME = paths.UP_NAME
RECORD_NAME = paths.LEGACY_RECORD_NAME
SKIP_NAME = paths.LEGACY_SKIP_NAME
LIST_NAME = paths.LEGACY_LIST_NAME

log = kit_log.log
tasks_mod = tasks


# ---------------- 数据目录的别名(保持老调用点可用) ----------------
#
# 以前这些是模块级常量, 现在统一由 paths 算; 这里用函数式的访问点,
# 免得"改了 DATA_ROOT 但别处还拿着旧值"。

def _data_root():
    return paths.data_root()


# ---------------- 菜单 ----------------

# 方向键/翻页键的扫描码 -> 语义. Windows 下这些键先给 0x00 或 0xE0,
# 再跟一个扫描码; 而且**两套前缀都可能出现**(取决于键盘/终端), 所以两种都认。
SPECIAL_KEYS = {
    "H": "up",          # ↑
    "P": "down",        # ↓
    "K": "left",        # ←
    "M": "right",       # →
    "I": "pageup",      # PgUp
    "Q": "pagedown",    # PgDn
    "G": "home",        # Home
    "O": "end",         # End
    "R": "insert",
    "S": "delete",
}


def read_key():
    """读一个键, 返回语义化名字.

    修过一个"按什么键都跳到最底下"的问题, 值得记一笔: 那**不是**这里的锅
    (映射表本身是对的, ↑↓ 的四种编码都认), 而是一次性打印 155 行把光标
    顶出了屏幕。所以看到"光标乱跳"先怀疑显示, 别急着改这里。
    """
    try:
        import msvcrt
    except Exception:
        return None
    try:
        ch = msvcrt.getwch()
    except Exception:
        return None
    if ch in ("\xe0", "\x00"):
        try:
            ch2 = msvcrt.getwch()
        except Exception:
            return None
        return SPECIAL_KEYS.get(ch2)
    if ch in ("\r", "\n"):
        return "enter"
    if ch == " ":
        return "space"
    if ch == "\x1b":            # Esc: 当成"什么都不做", 免得被当成字母
        return None
    return ch.lower()


MENU_ACTIONS = [
    ("A", "一键更新全部"),
    ("N", "下载新UP主"),
    ("S", "下载单个视频"),
    ("C", "下载合集/系列(粘贴合集链接)"),
    ("R", "重新下载所有已跳过项目"),
    ("F", "全部完整校验(慢, 排查用)"),
    ("D", "冻结/启用光标所在的那个UP主(暂停更新与下载)"),
    ("Q", "退出"),
]


def console_rows(default=40):
    """控制台能显示多少行.

    取不到真实高度时给 40(而不是 25): 现在只画光标附近那一屏, 窗口开小一点
    没坏处, 但**给太小会让列表只剩几行**, 翻起来很烦。
    """
    try:
        import shutil
        return max(12, shutil.get_terminal_size((80, default)).lines)
    except Exception:
        return default


def menu_window(folders, cursor, frozen):
    """算出现在该显示哪一段 —— 这是修"光标看不见"的关键.

    以前是一次性把**所有** UP主都打印出来(155 行!), 控制台一滚到底,
    而 `>` 光标在列表顶上、早就出了屏幕 —— 于是看起来"光标不在, 按什么键
    都跑到最底下"。现在只画光标附近的一屏, 光标永远在可见范围内。

    返回 (起始下标, 结束下标); 上下还有多少用页眉页脚说明。
    """
    total = len(folders)
    if total <= 0:
        return 0, 0
    # 菜单本身要占掉的行数(标题 + 按键说明 + 空行 + 状态行 + 页脚)
    chrome = len(MENU_ACTIONS) + 9
    rows = max(3, console_rows() - chrome)
    if total <= rows:
        return 0, total
    # 让光标大致居中, 但不越界
    half = rows // 2
    start = max(0, min(cursor - half, total - rows))
    return start, min(total, start + rows)


def menu_lines(folders, cursor, selected, frozen, notice=""):
    """整个菜单画面的每一行(纯函数: 只算内容, 不管怎么显示).

    抽出来是为了两件事:
      · 显示方式可以换(以前 os.system("cls")+print, 现在原地重写不闪),
        而**画面内容一个字都不用动**
      · 内容能被测试(哪个选项被选中、光标在哪一行、冻结标记在不在)
    """
    start, end = menu_window(folders, cursor, frozen)
    out = [
        "=" * 54,
        "   BBDown 下载管理器 - 选择要更新的UP主",
        "=" * 54,
        "  [↑/↓] 移动  [PgUp/PgDn] 翻页  [Home/End] 头/尾  "
        "[空格] 选择/取消  [回车] 开始更新",
    ]
    for key, what in MENU_ACTIONS:
        out.append("  [%s] %s" % (key, what))
    out.append("")
    if start > 0:
        out.append("  ↑ 上面还有 %d 个" % start)
    for i in range(start, end):
        name = folders[i]
        mark = ">" if i == cursor else " "
        sel = "x" if i in selected else " "
        count = record_count(os.path.join(_data_root(), name))
        tag = "  [已冻结, 不会更新]" if name in frozen else ""
        out.append(" %s [%s] %2d. %s  (本地记录 %d 个)%s"
                   % (mark, sel, i + 1, os.path.basename(name), count, tag))
    if end < len(folders):
        out.append("  ↓ 下面还有 %d 个" % (len(folders) - end))
    out.append("")
    out.append(" 已选择 %d 个UP主 / 共 %d 个%s"
               % (len(selected), len(folders),
                  ("，已冻结 %d 个" % len(frozen)) if frozen else ""))
    if notice:
        out.append(" " + notice)
    return out, (start, end)


def select_up_folders(folders):
    """交互式选择; 不是终端(比如被守护拉起)就走输入编号那条路.

    画面交给 menu_lines(), 刷新交给 console.Screen(原地重写).
    以前每按一个键就 os.system("cls") 再逐行 print —— cls 会先把整个屏幕擦白,
    中间那张白脸就是闪烁的来源。
    """
    if not sys.stdin.isatty():
        return select_folders_input(folders)
    from bbdown_kit import freeze
    from bbdown_kit.console import Screen, restore_cursor
    cursor = 0
    selected = set()
    notice = ""
    frozen = freeze.frozen_set()
    screen = Screen()
    span = 1
    try:
        while True:
            lines, (start, end) = menu_lines(folders, cursor, selected,
                                             frozen, notice)
            span = max(1, end - start)
            screen.begin()
            screen.lines_from(lines)
            screen.end()

            key = read_key()
            notice = ""
            if key == "up":
                cursor = (cursor - 1) % len(folders)
            elif key == "down":
                cursor = (cursor + 1) % len(folders)
            elif key == "pageup":
                cursor = max(0, cursor - span)
            elif key == "pagedown":
                cursor = min(len(folders) - 1, cursor + span)
            elif key == "home":
                cursor = 0
            elif key == "end":
                cursor = len(folders) - 1
            elif key == "space":
                selected.symmetric_difference_update({cursor})
            elif key == "d":
                # 就地冻结/启用光标所指的那个, 不退出菜单
                target = folders[cursor]
                if target in frozen:
                    ok, why = freeze.unfreeze(target)
                else:
                    ok, why = freeze.freeze(target, "在菜单里冻结")
                notice = "%s: %s" % (why, os.path.basename(target))
                selected.discard(cursor)      # 状态变了, 顺手取消选择
                frozen = freeze.frozen_set()
            elif key == "enter":
                if not selected:
                    notice = "请至少选择一个UP主!"
                    continue
                picked = [folders[i] for i in sorted(selected)]
                skipped = [p for p in picked if p in frozen]
                if skipped:
                    # 冻结的名单就算被选中也不下 —— 明说, 免得你以为它下了
                    if not frozen_warning(screen, lines, skipped):
                        continue
                    picked = [p for p in picked if p not in frozen]
                    if not picked:
                        continue
                return picked
            elif key in ("a", "n", "s", "c", "r", "f"):
                return {"a": "ALL", "n": "NEW", "s": "SINGLE",
                        "c": "COLLECTION", "r": "RETRY_SKIP", "f": "FULL"}[key]
            elif key == "q":
                log("已退出")
                sys.exit(0)
    finally:
        # 退出前把光标放回去, 否则控制台里光标就没了(看起来像卡死)
        restore_cursor()


def frozen_warning(screen, lines, skipped):
    """选中的里面有冻结的: 在**同一块画面**里问一句, 别用 input() 开新行.

    以前这里用 print + input(), 会跑到画面下方去, 退出后还得整屏重画(又是一次
    闪烁)。现在把提示接在画面末尾、原地重写一次, 然后等一个键 —— 画面不跳。

    lines 传的是**当前已经画出来的那几行**(menu_lines 的产物), 不是从
    screen 里读 —— screen.begin() 会把缓冲清掉, 读不到旧内容。
    """
    names = "、".join(os.path.basename(s) for s in skipped)[:60]
    screen.begin()
    screen.lines_from(list(lines))
    screen.line("")
    screen.line(" 有 %d 个是已冻结的, 不会被更新: %s" % (len(skipped), names))
    screen.line(" 要一起更新就先按 D 启用它们")
    screen.line(" 按回车继续(只更新没冻结的), 按 Q 返回菜单")
    screen.end()
    while True:
        key = read_key()
        if key == "enter":
            return True
        if key in ("q", None):
            return False


def select_folders_input(folders):
    """没有交互终端时的退化菜单(输入编号)."""
    from bbdown_kit import freeze
    frozen = freeze.frozen_set()
    print()
    print("检测到已有UP主文件夹, 选择要更新的UP主:")
    for i, name in enumerate(folders, 1):
        count = record_count(os.path.join(_data_root(), name))
        tag = "  [已冻结, 不会更新]" if name in frozen else ""
        print("  %d. %s  (本地记录 %d 个)%s"
              % (i, os.path.basename(name), count, tag))
    print("  (输入编号, 多个用逗号分隔, 例如: 1,3)")
    print("  (输入 A 一键更新全部, 输入 N 下载新UP主, 输入 S 下载单个视频, "
          "输入 C 下载合集/系列)")
    print("  (输入 R 重新下载所有已跳过项目, 输入 F 全部完整校验)")
    print("  (冻结/启用某个UP主: 输入 D<编号>, 例如 D3; 输入 DL 看清单)")
    mapping = {"a": "ALL", "n": "NEW", "s": "SINGLE", "c": "COLLECTION",
               "r": "RETRY_SKIP", "f": "FULL"}
    while True:
        text = input("> ").strip()
        low = text.lower()
        if low in mapping:
            return mapping[low]
        if low == "dl":
            print("\n".join(freeze.all_report_lines()))
            continue
        if low.startswith("d") and low[1:].strip().isdigit():
            idx = int(low[1:].strip())
            if not (1 <= idx <= len(folders)):
                print("编号 %d 超出范围" % idx)
                continue
            target = folders[idx - 1]
            if target in freeze.frozen_set():
                ok, why = freeze.unfreeze(target)
            else:
                ok, why = freeze.freeze(target, "在菜单里冻结")
            print("%s: %s" % (why, os.path.basename(target)))
            frozen = freeze.frozen_set()
            continue
        try:
            idxs = [int(x) for x in re.split(r"[,，\s]+", text) if x.strip()]
        except ValueError:
            print("输入有误, 请重新输入")
            continue
        picked = []
        ok = True
        for i in idxs:
            if 1 <= i <= len(folders):
                if folders[i - 1] not in picked:
                    picked.append(folders[i - 1])
            else:
                ok = False
                print("编号 %d 超出范围" % i)
        if ok and picked:
            # 冻结的就算被选中也不下 —— 这里明确剔掉并说一声
            skipped = [p for p in picked if p in freeze.frozen_set()]
            if skipped:
                print("有 %d 个已冻结, 本次不更新: %s"
                      % (len(skipped), "、".join(os.path.basename(s)
                                                 for s in skipped)[:60]))
                picked = [p for p in picked if p not in freeze.frozen_set()]
            if picked:
                return picked
            continue
        print("请至少选择一个有效编号")


def _ask(prompt, hint=None, validate=None, error=None):
    """问一个链接并校验; Ctrl+C / EOF 直接退出."""
    print()
    print(prompt)
    if hint:
        print(hint)
    while True:
        try:
            text = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)
        if validate is None or validate(text):
            return text
        print()
        print(error or "无法识别, 请重新输入")


def ask_up_link():
    """问一个 UP 主主页链接, 返回 **UID**(纯数字).

    注意这里必须 `return extract_mid(...)`: 以前只把 extract_mid 用在**校验**上,
    返回的却是用户输入的**原文**(整条链接), 于是整条 URL 被当成 UID 传到
    acc/info 接口, 接口反问"这是啥"返回 -400, 界面上报成"登录已失效" ——
    用户白跑一趟去扫码, 其实登录好好的。
    """
    text = _ask("请输入您将要下载视频的UP主的主页链接:",
                "例如: https://space.bilibili.com/123456780",
                lambda t: bilitools.extract_mid(t),
                "输入格式有误, 请重新输入正确的格式"
                "(UP主主页链接形如 space.bilibili.com/数字)")
    return bilitools.extract_mid(text) or text.strip()


def ask_video_link():
    return _ask("请输入单个视频链接:",
                "例如: https://www.bilibili.com/video/BV1xx... 或 av数字 或 b23.tv短链",
                lambda t: bilitools.classify_link(t) == "video",
                "无法识别视频链接, 请重新输入")


def ask_collection_link():
    return _ask("请输入合集/系列链接(在合集页面直接复制地址栏):",
                "例如: https://space.bilibili.com/123456781/lists/1000001?type=season\n"
                "      https://space.bilibili.com/1234567/channel/collectiondetail?sid=8888",
                bilitools.parse_collection_link,
                "无法识别合集链接, 请粘贴形如 "
                "space.bilibili.com/<UID>/lists/<合集号> 的地址")


def ask_link():
    """不确定是哪种链接时, 让用户粘一条, 程序自己认."""
    print()
    print("请输入链接:")
    print("  - UP主主页: https://space.bilibili.com/数字")
    print("  - 单个视频: https://www.bilibili.com/video/BVxxxx / av数字 / b23.tv短链")
    print("  - 合集/系列: https://space.bilibili.com/数字/lists/合集号?type=season")
    while True:
        try:
            text = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)
        # 合集链接也是 space.bilibili.com/数字/... , 要先认合集再看UP主
        if bilitools.parse_collection_link(text):
            return ("collection", text)
        kind = bilitools.classify_link(text)
        if kind == "up":
            return ("up", bilitools.extract_mid(text))
        if kind == "video":
            return ("video", text)
        print()
        print("无法识别该链接, 请重新输入")


def confirm_download(folder, count, preview):
    """下载前的确认. 参数顺序和历史调用点一致(folder 只用于显示)."""
    print()
    print("即将下载: %s" % os.path.basename(str(folder).rstrip("\\/")))
    print("本次需要下载 %d 个新视频:" % count)
    for title in preview:
        print("  - %s" % highlight(title))
    if count > len(preview):
        print("  ... 等共 %d 个" % count)
    while True:
        try:
            ans = input("确认开始下载? (Y=下载 / N=跳过该UP主): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if ans in ("y", "yes", ""):
            return True
        if ans in ("n", "no"):
            return False
        print("请输入 Y 或 N")


class ConsoleContext(object):
    """download 模块需要的回调(问用户). --yes 时不问."""

    def confirm(self, folder, count, preview):
        return confirm_download(folder, count, preview)


# ---------------- 单个视频 ----------------

def process_single_video(session, video_text, args, stats):
    """菜单按 S / --video: 解析链接 -> 登记进名单 -> 下载一个视频."""
    print()
    log("正在解析视频链接...")
    info = bilitools.resolve_video(session, video_text)
    if not info:
        print()
        print("未找到该视频(链接可能有误, 或视频已删除/仅限内部)")
        stats["failed"] += 1
        return
    log("视频: %s [%s]" % (highlight(info["title"]), info["aid"]))
    log("保存到: %s" % paths.single_dir())

    st = download.register_single_video(info)
    if st.is_done(info["aid"]):
        log("该视频之前已下载过")
        if not args.yes and not _yes_no("仍要重新下载吗? (Y/N): "):
            log("已取消")
            return
    if not args.yes and not _yes_no("确认开始下载? (Y=下载 / N=取消): "):
        log("已取消")
        return

    if download.download_single_now(st, info, args, stats):
        log("下载完成 ✓ 已保存到: %s" % paths.single_dir())
    else:
        log("下载失败 ✗ (可能是充电/会员专属视频; 已记下, 下次跑守护会自动重试)")


def _yes_no(prompt):
    try:
        ans = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans in ("y", "yes", "")


# ---------------- 主流程 ----------------

def build_parser():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--url", default=None)
    ap.add_argument("--video", default=None)
    ap.add_argument("--collection", default=None,
                    help="合集/系列链接(space.bilibili.com/<UID>/lists/<合集号>)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--parallel", type=int, default=None)
    ap.add_argument("--interval", type=float, default=None)
    ap.add_argument("--fail-threshold", type=int, default=None)
    ap.add_argument("--pause-seconds", type=int, default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--order", default=None,
                    help="这一轮先处理谁: collection_first/smallest/largest/name"
                         "/duration_small/duration_large(按总时长排)")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--retry-skip", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--full-days", type=int, default=None)
    ap.add_argument("--bbdown-args", default="")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--event-log", default=None)
    ap.add_argument("--lock-held", action="store_true",
                    help="互斥锁已由下载守护持有, 本程序不再加锁")
    ap.add_argument("--download-timeout", type=int, default=None,
                    help="单个视频最多下几分钟, 0=不限时")
    return ap


def apply_settings(args, settings):
    """命令行 > 下载设置.txt > 内置默认值."""
    args.parallel = args.parallel or settings["parallel"]
    args.interval = (args.interval if args.interval is not None
                     else settings["interval"])
    args.fail_threshold = (args.fail_threshold
                           if args.fail_threshold is not None
                           else settings["fail_threshold"])
    args.pause_seconds = (args.pause_seconds
                          if args.pause_seconds is not None
                          else settings["pause_seconds"])
    args.full_days = (args.full_days if args.full_days is not None
                      else settings["full_days"])
    timeout_min = (args.download_timeout if args.download_timeout is not None
                   else settings["download_timeout_minutes"])
    args.download_timeout = max(0, int(timeout_min)) * 60 or None   # 秒, 0=不限时
    args.order = orders.normalize_order(args.order or settings["order"])
    return args


class Plan(object):
    """这一轮要干什么: 一批任务 + 可能的一个链接."""

    __slots__ = ("tasks", "video", "collection")

    def __init__(self, tasks=None, video=None, collection=None):
        self.tasks = list(tasks or [])
        self.video = video
        self.collection = collection

    def empty(self):
        return not self.tasks and not self.video and not self.collection


def ordered_tasks(up_tasks, coll_tasks, single_tasks, args):
    """按 order 排好这一轮的任务, 并把"先跑谁"写进日志.

    "一键更新/守护"和菜单里的『全部完整校验』『重新下载所有已跳过项目』都走
    这里 —— 以前后两个菜单项直接用文件夹名顺序, 于是 下载设置.txt 里的 order
    设了也不生效(选了按时长排也只有一键更新那条路才按时长)。
    """
    ordered = tasks_mod.order_tasks(up_tasks, coll_tasks, single_tasks, args.order)
    by_duration = orders.is_duration_order(args.order)
    log("这一轮的处理顺序(%s): %s"
        % (orders.order_text(args.order),
           tasks_mod.order_preview(ordered, durations=by_duration)))
    if by_duration:
        # 按时长排专用的交代: 一共要下多久、有多少名单还没有时长数据。
        # 这句是给人核对"顺序对不对、数据齐不齐"用的 —— 只影响先跑谁, 不影响下不下。
        total, work, blank = duration.stats([tasks_mod.task_folder(t)
                                             for t in ordered])
        if duration.typical_seconds() <= 0:
            # 全库一个时长都没有(老状态文件就是这样): 这时候那个"合计"只是把
            # 缺口条数当秒数加了起来, 报出来会让人以为真的只要十几个小时 ——
            # 不如直接说清"现在还没数据、先按条数排"。
            log("时长统计: %d 个名单还有得下, 但一个时长数据都还没有 —— 这一轮"
                "先按缺口条数排; 等各名单下次完整校验(默认 7 天一轮)之后就有数了。"
                "想现在补齐: 加 --full --limit 0 跑一遍 = 只刷列表不下载" % work)
        else:
            log("时长统计: %d 个名单还有得下, 合计约 %s (时长来自列表接口, 顺路存的)"
                % (work, duration.describe(total)))
            if blank:
                log("其中 %d 个名单还没有时长数据(下次完整校验补上, 在那之前它们"
                    "按缺口条数排)" % blank)
    return ordered


def plan_all(args, reason):
    """一键更新: 三类名单全都要."""
    up_tasks, coll_tasks, single_tasks = tasks_mod.build_all_tasks()
    if not up_tasks and not coll_tasks and not single_tasks:
        print("没有找到可更新的UP主文件夹, 也没有已登记的合集")
        sys.exit(1)
    args.yes = True
    print()
    log("已选择%s %d 个UP主, 全程不再询问" % (reason, len(up_tasks)))
    if coll_tasks:
        log("另有 %d 个合集/系列一起检查更新" % len(coll_tasks))
    if single_tasks:
        log("另有单视频下载里的 %d 个零散视频一起检查"
            % single_tasks[0][1]["count"])
    return Plan(tasks=ordered_tasks(up_tasks, coll_tasks, single_tasks, args))


def collect_tasks(args, session):
    """决定这一轮要处理什么(命令行参数优先, 否则问用户)."""
    if args.all:
        return plan_all(args, "一键更新全部")
    if args.video:
        return Plan(video=args.video)
    if args.collection:
        return Plan(collection=args.collection)
    if args.url:
        # 合集链接也长成 space.bilibili.com/数字/... , 必须先认合集再当UP主
        if bilitools.parse_collection_link(
                bilitools.resolve_short_link(session, args.url)):
            return Plan(collection=args.url)
        mid = bilitools.extract_mid(args.url)
        if not mid:
            print("链接格式有误: %s" % args.url)
            sys.exit(1)
        return Plan(tasks=[("new", mid)])

    folders = tasks_mod.scan_up_folders()
    if not folders:
        kind, value = ask_link()
        if kind == "up":
            return Plan(tasks=[("new", value)])
        if kind == "collection":
            return Plan(collection=value)
        return Plan(video=value)

    choice = select_up_folders(folders)
    if choice == "NEW":
        return Plan(tasks=[("new", ask_up_link())])
    if choice == "SINGLE":
        return Plan(video=ask_video_link())
    if choice == "COLLECTION":
        return Plan(collection=ask_collection_link())
    if choice == "ALL":
        return plan_all(args, "一键更新全部")
    if choice in ("RETRY_SKIP", "FULL"):
        if choice == "RETRY_SKIP":
            args.retry_skip = True
            what = "重新下载所有已跳过项目"
        else:
            args.full = True
            what = "完整校验全部投稿列表"
        args.yes = True
        picked = [("existing", f) for f in folders if tasks_mod.folder_to_mid(f)]
        log("已选择%s (共 %d 个UP主), 全程不再询问" % (what, len(picked)))
        # 和"一键更新"一样按设置里的 order 排(以前这里直接用文件夹名顺序,
        # 于是选了按时长排也只有"一键更新/守护"那条路才生效)。
        return Plan(tasks=ordered_tasks(picked, [], [], args))
    return Plan(tasks=[("existing", f) for f in choice
                       if tasks_mod.folder_to_mid(f)])


def run_tasks(session, key, tasks, video_text, collection_text, args, stats, ctx):
    """按顺序执行：单个视频 -> 合集 -> 各UP主."""
    if video_text:
        process_single_video(session, video_text, args, stats)

    if collection_text:
        print()
        print("-" * 54)
        log("正在识别合集/系列链接...")
        try:
            spec = bilitools.resolve_collection(session, collection_text)
        except Exception as e:
            log("解析合集链接出错: %s" % e)
            spec = None
        if not spec:
            print()
            print("无法识别该合集链接: %s" % collection_text)
            print("支持的写法: space.bilibili.com/<UID>/lists/<合集号>?type=season"
                  "(或 collectiondetail?sid=, seriesdetail?sid=)")
            stats["failed"] += 1
        else:
            log("%s: %s [%s]"
                % (spec["类型"], highlight(spec["名称"]), spec["id"]))
            log("合集里有 %d 个视频" % len(spec["视频列表"]))
            download.process_collection(session, spec, args, stats, ctx=ctx)

    for done_count, (kind, item) in enumerate(tasks):
        # 优雅停止: 收到请求就不再开始"下一个文件夹/下一个UP主"。
        # 注意检查点放在这里(每个任务之前), 不是放在下载中途 —— 中途那层由
        # lockstep.run_batch 负责, 它保证在下的视频下完、只是不再取新的。
        if procs.graceful_stop_requested():
            log("收到优雅停止请求: 剩下的 %d 个任务这次不做了(下次运行接着做)"
                % (len(tasks) - done_count))
            break
        print()
        print("-" * 54)
        if kind == "collection":
            download.process_collection(session, item["spec"], args, stats,
                                        folder=item["folder"], ctx=ctx)
            continue
        if kind == "single":
            download.process_single_folder(args, stats, folder=item["folder"],
                                           ctx=ctx)
            continue
        if kind == "new":
            # 兜底: 不管从菜单还是 --url 进来, UID 位置上必须只剩纯数字。
            # 以前这里直接用 item, 于是"整条链接被当 UID"能一路传到接口,
            # 报出来的错还指向别的地方(见 ask_up_link 的说明)。
            mid = bilitools.extract_mid(str(item))
            if not mid:
                print()
                print("认不出这个 UID/链接: %s" % item)
                print("UP主主页链接形如 https://space.bilibili.com/1234567,")
                print("也可以直接输数字 UID。")
                stats["failed"] += 1
                continue
            log("正在识别UP主信息 (UID=%s) ..." % mid)
            up_name = None
            why = ""
            try:
                up_name, why = bilitools.get_up_info(session, key, mid)
            except Exception as e:
                why = "请求出错: %s" % e
            if up_name is None:
                # 以前这里一律说"未找到该UP主" —— 于是被限流也被说成"没这个人",
                # 用户会以为链接写错了。现在按真实原因分开说。
                print()
                if "限流" in why or "频繁" in why:
                    print("暂时拿不到该UP主信息: %s" % why)
                    print("这不是链接的问题 —— 是账号现在被 B 站限流了。")
                    print("处理: 等几分钟再试; 或者直接双击 启动下载守护.bat,")
                    print("      它撞到限流会自己休息、重试, 也会把该补的补齐。")
                elif "登录" in why:
                    print("暂时拿不到该UP主信息: %s" % why)
                    print("处理: 双击 BBDown-身份登录.bat 重新扫码, 然后再试。")
                elif "确实不存在" in why:
                    print("查无此人: UID %s 对应的账号不存在(可能已注销/被封)。" % mid)
                    print("链接: https://space.bilibili.com/%s" % mid)
                else:
                    print("暂时拿不到该UP主信息: %s" % why)
                    print("可以先在浏览器里打开 https://space.bilibili.com/%s"
                          " 确认账号还在, 然后重试。" % mid)
                stats["failed"] += 1
                continue
            folder = tasks_mod.up_folder_for(mid, up_name)
            log("UP主: %s (%s)" % (up_name, mid))
            log("专属文件夹: %s" % folder)
        else:
            folder = os.path.join(_data_root(), item)
            mid = tasks_mod.folder_to_mid(item)
            if mid is None:
                continue
        download.process_up(session, key, folder, mid, args, stats, ctx=ctx)


def split_extra_args(text):
    """--bbdown-args 的字符串 -> 参数列表(按 Windows 习惯切).

    以前用 shlex.split 的 POSIX 模式: 反斜杠被当成转义符, "--work-dir
    C:\\temp\\x" 会变成 "C:tempx"; 引号不配对时还抛异常打出一整串栈。
    这里按 Windows 规则切: 反斜杠就是反斜杠, 只去掉最外层成对的引号。
    """
    text = str(text or "")
    if not text.strip():
        return []
    try:
        parts = shlex.split(text, posix=False)
    except ValueError:
        parts = text.split()
    result = []
    for part in parts:
        if len(part) >= 2 and part[0] == part[-1] and part[0] in "\"'":
            part = part[1:-1]
        result.append(part)
    return result


def _merge_value_args(argv):
    """把 "--bbdown-args X" 合写成 "--bbdown-args=X".

    argparse 会把 "-h" 这种以横线开头的值当成选项, 于是
    `--bbdown-args "-h"` 直接报 "expected one argument"。合写之后这个值原样
    交给 BBDown(比如想看 BBDown 自己的帮助), 而**单独**的 -h 仍然是
    "看本程序的帮助"。
    """
    out = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--bbdown-args" and index + 1 < len(argv):
            out.append("--bbdown-args=%s" % argv[index + 1])
            index += 2
            continue
        out.append(item)
        index += 1
    return out


def _sweep_workdirs():
    """清掉下载被掐断后残留的 <aid> 临时目录(规则见 bbdown_kit/workdirs.py).

    BBDown 正常下完会自己清; 剩下的都是超时/限流/接管/Ctrl+C 留下的半成品,
    而它没有断点续传 —— 留着只是白占磁盘。清理出问题也绝不影响下载本身,
    所以这里兜住所有异常。
    """
    try:
        report = workdirs.sweep()
    except Exception as e:
        log("清理下载残留出错(忽略): %r" % e)
        return
    if report["dirs"]:
        log("清掉 %d 个下载残留的临时目录 (释放 %s)"
            % (report["cleaned"], size_text(report["freed"])))


def run_manager():
    parser = build_parser()
    argv = _merge_value_args(sys.argv[1:])
    if "--help" in argv or "-h" in argv:
        print("用法: 双击本程序进菜单; 下面的参数是给脚本/下载守护用的。")
        parser.print_help()
        return 0
    args, unknown = parser.parse_known_args()
    if unknown:
        # 以前直接丢掉: --paralell 这种拼错会静默按默认值跑, 看起来"设置没生效"
        log("不认识这些参数, 已忽略: %s" % " ".join(unknown))
    args.extra_args = split_extra_args(args.bbdown_args)

    settings = config.load_settings()
    paths.refresh_data_root(settings["data_root"])
    args = apply_settings(args, settings)

    print("=" * 54)
    print("   BBDown 下载管理器")
    print("=" * 54)
    log("视频目录: %s" % paths.data_root())
    log("处理顺序: %s (下载设置.txt 的 order, 可用 --order 临时改)"
        % orders.order_text(args.order))

    if not os.path.exists(paths.bbdown_exe()):
        print("未找到 BBDown.exe, 请把本程序放在 BBDown.exe 所在目录")
        sys.exit(1)

    session = bilitools.make_session()
    log("检查登录状态...")
    # 撞上限流/网络抖动时, 先自己退避重试几次再下结论 —— 否则会让人白跑一趟
    # 去扫码(以前只查一次, 一次失败就说"登录已失效")。
    logged_in, why = False, ""
    for attempt in range(3):
        logged_in, why = bilitools.login_verdict(session)
        if logged_in:
            break
        if not ("限流" in why or "网络" in why):
            break                      # 真过期/真异常: 重试也没用
        if attempt < 2:
            wait = 5 * (attempt + 1)
            log("登录检查未通过(%s), %d 秒后重试(%d/3)" % (why, wait, attempt + 1))
            time.sleep(wait)
    if not logged_in:
        kit_log.emit("login_failed", reason=why)
        print()
        if "限流" in why:
            # 登录好着呢, 只是被限流 —— 别让人去扫码
            print("暂时没法确认登录状态: %s" % why)
            print("你的登录**没有过期**(SESSDATA 有效期到 %s)"
                  % (bilitools.sessdata_expiry() or "读不出来"))
            print("这是 B 站限流, 不是账号问题。处理:")
            print("  · 等几分钟再运行本程序; 或者")
            print("  · 直接双击 启动下载守护.bat —— 它撞到限流会自己休息重试")
        elif "网络" in why:
            print("连不上 B 站: %s" % why)
            print("检查一下网络/代理, 然后重试。")
        else:
            print("当前未登录或登录已失效: %s" % why)
            print("请先双击 BBDown-身份登录.bat 扫码登录, 然后再运行本程序")
        sys.exit(1)
    log("登录状态正常")
    expiry = bilitools.sessdata_expiry()
    if expiry:
        log("cookie 有效期到 %s" % expiry.strftime("%Y-%m-%d %H:%M:%S"))

    try:
        key = bilitools.require_mixin_key(session)
    except Exception as e:
        print()
        print("拿不到接口签名密钥, 本次不下载 (%s)" % e)
        print("多半是网络不通或被风控: 过一会儿再试; 一直这样就先双击")
        print("BBDown-身份登录.bat 看看登录状态")
        return 1
    stats = {"downloaded": 0, "failed": 0}
    ctx = ConsoleContext()
    plan = collect_tasks(args, session)

    if plan.empty():
        print("没有需要处理的任务")
        sys.exit(1)

    if "--lock-held" not in sys.argv[1:]:
        # 不是守护拉起来的(用户自己在用本程序): 守护每轮开跑前都会扫一遍下载
        # 残留, 这里补上同样的扫描 —— 只用管理器的人也才不会又把 <aid> 临时
        # 目录攒起来。守护跑的时候跳过, 免得同一件事做两遍。
        _sweep_workdirs()

    run_tasks(session, key, plan.tasks, plan.video, plan.collection,
              args, stats, ctx)

    print()
    print("=" * 54)
    log("全部任务结束")
    log("本次新下载: %d 个" % stats["downloaded"])
    kit_log.emit("run_end", downloaded=stats["downloaded"],
                 failed=stats["failed"])
    if stats["failed"]:
        log("下载失败: %d 个" % stats["failed"])
    else:
        log("下载失败: 0 个")
    return exit_code(stats)


def exit_code(stats):
    """按本次结果决定退出码 —— 这是"有没有出事"的唯一信号.

    以前这里无条件返回 0: 于是定时任务(定时更新全部博主.bat)不管下成没下成
    都写 "Finished with exit code 0"、计划任务的 LastTaskResult 也是 0,
    看起来永远成功 —— 唯一的自动告警信号就这么没了。

    约定(务必和 定时更新全部博主.bat / 守护 保持一致):
        0  全部成功(或者本来就没有需要下载的)
        1  有视频下载失败(部分成功也算, 因为失败的那些要人来看看)
        2  一个都没成功, 而且确实失败了 —— "全军覆没", 通常是被限流/登录失效
    退出码 1 和 2 会被 .bat 透传给计划任务, 方便任务计划程序标红/发通知。
    """
    failed = int(stats.get("failed") or 0)
    downloaded = int(stats.get("downloaded") or 0)
    if failed and not downloaded:
        return 2
    if failed:
        return 1
    return 0


def main():
    kit_log.setup_console()
    kit_log.peek_event_file(sys.argv[1:])
    # 守护起的本程序: 互斥由守护全程代持, 这里不再加锁
    if "--lock-held" in sys.argv[1:]:
        log("互斥锁由下载守护持有, 本管理器不再单独加锁")
    else:
        holder = procs.manager_lock.acquire()
        if holder:
            kit_log.emit("lock_busy", pid=os.getpid())
            print()
            print("检测到另一个下载管理器正在运行")
            print("同一个账号任何时候只允许一个下载进程。")
            print("如果那是下载守护, 让它跑就行(它会把该补的都补上);")
            print("否则请先关闭之前的窗口/等它结束, 然后再启动本程序")
            return 1
    kit_log.emit("run_start", pid=os.getpid())
    try:
        return run_manager()
    finally:
        procs.manager_lock.release()


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        print()
        log("已取消操作")
        sys.exit(130)
