# -*- coding: utf-8 -*-
"""清点: 哪些是"工具"(不许动), 哪些是"我们的代码"(可改可删).

为什么要有这个文件 —— 一次真实的教训:
    收尾清理时看到 ffmpeg 发行版里的 ffplay.exe(187 MB), 理由是
    "BBDown.exe 里 ffplay 出现 0 次, 代码零引用, 用不到", 于是删了。
    **这个理由本身没错, 但前提错了**: ffplay 不是我们的冗余代码, 它是
    FFmpeg 发行版自带的一个工具。少一个文件, 这个包就不再是那个发行版,
    用户哪天想用它试播个文件、或者想离线查参数, 就没有了。
    doc\\ 和 presets\\ 同理。

所以定下规矩:
    工具部分 —— 原样保留。只允许"更新到新版本"(整包替换), 不允许删里面的文件。
    我们的代码 —— 可以改、可以删, 但删之前必须能说出是哪一条:
                    (a) 功能不要了
                    (b) 已经被别的东西取代了
                    (c) 结构上不再需要了(重构后没有调用点)
               三条都说不出, 就是没想清楚, 不该删。

这个脚本把划分**写死在代码里**, 而不是靠记忆。任何时候跑一下:

    python tools\\清点工具与代码.py

它会告诉你: 工具有没有缺件、有没有多出不该在工具目录里的东西、
我们的代码有多少。它**只读, 什么都不改**。

判断"哪些目录算工具"用的是 tools/工具清单.txt(可改), 而不是硬编码 ——
以后你换了 ffmpeg 版本(比如解压成 ffmpeg-9.0-full_build), 改那个清单即可。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bbdown_kit import paths                     # noqa: E402
from bbdown_kit.logging import setup_console      # noqa: E402


def read_manifest():
    """读 tools\\工具清单.txt: 每行 `路径<TAB>说明`, # 开头是注释."""
    path = os.path.join(paths.PROJ, "工具清单.txt")
    rows = []
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t", 1)
                rows.append((parts[0].strip(),
                             parts[1].strip() if len(parts) > 1 else ""))
    except OSError:
        pass
    return rows, path


def dir_stats(full):
    n = 0
    size = 0
    for base, _dirs, files in os.walk(full):
        for f in files:
            n += 1
            try:
                size += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
    return n, size


def baseline_path():
    return os.path.join(paths.PROJ, "工具基线.json")


def read_baseline():
    """工具的文件数基线.

    为什么要它: 清单只能核对"顶层还在不在", 而发行版**内部**少一个文件
    (比如上次误删的 bin\\ffplay.exe)它是看不见的。基线记下每个工具目录
    "本来有多少个文件", 下次一跑就知道少了没有。

    这个文件由本脚本自动维护: 第一次跑会建它; 你换了新版本之后,
    加 --记基线 让它重新记一次。
    """
    from bbdown_kit import state as state_mod
    data = state_mod.load_json(baseline_path(), None)
    return data if isinstance(data, dict) else {}


def write_baseline(data):
    from bbdown_kit import state as state_mod
    return state_mod.save_json_atomic(baseline_path(), data)


def main():
    setup_console()
    refresh = "--记基线" in sys.argv
    rows, manifest_path = read_manifest()
    if not rows:
        print("读不到工具清单: %s" % manifest_path)
        print("(这个文件列着哪些是第三方工具, 缺了就没法判断 —— 别删它)")
        return 2

    baseline = read_baseline()
    new_baseline = {}

    print("=" * 74)
    print("第三方工具(规矩: 原样保留, 不删里面任何文件)")
    print("=" * 74)
    tool_files = 0
    tool_bytes = 0
    problems = []
    for rel, what in rows:
        full = os.path.join(paths.PROJ, rel.replace("/", os.sep))
        if not os.path.exists(full):
            print("  [缺失] %-38s %s" % (rel, what))
            problems.append("缺失: %s" % rel)
            continue
        if os.path.isdir(full):
            n, size = dir_stats(full)
            tool_files += n
            tool_bytes += size
            new_baseline[rel] = n
            was = baseline.get(rel)
            note = ""
            if was is not None and n < was:
                note = "  << 少了 %d 个文件!" % (was - n)
                problems.append("%s 少了 %d 个文件(基线 %d, 现在 %d)"
                                % (rel, was - n, was, n))
            elif was is not None and n > was:
                note = "  (比基线多 %d 个; 换过版本就 --记基线 重记一次)" % (n - was)
            print("  %-38s %4d 个文件 %8.1f MB%s"
                  % (rel + "/", n, size / 1048576.0, note))
        else:
            size = os.path.getsize(full)
            tool_files += 1
            tool_bytes += size
            print("  %-38s      %8.1f MB" % (rel, size / 1048576.0))
        if what:
            print("        %s" % what)
    print()
    print("  合计 %d 个文件, %.1f MB" % (tool_files, tool_bytes / 1048576.0))

    ours, ours_bytes = inventory_ours(rows)
    print()
    print("=" * 74)
    print("我们自己的代码/文档(规矩: 可改可删, 但要有理由)")
    print("=" * 74)
    print("  %d 个文件, %.1f KB" % (len(ours), ours_bytes / 1024.0))
    print("  (占了项目体积的 %.1f%%; 其余都是上面的第三方工具)"
          % (ours_bytes * 100.0 / max(1, ours_bytes + tool_bytes)))

    stray = stray_files(rows)
    print()
    print("=" * 74)
    print("需要留意的")
    print("=" * 74)
    if stray:
        print("  这些文件躺在工具目录里, 但不在清单上:")
        for rel in stray:
            print("    %s" % rel)
        print("  要么把它加进 tools\\工具清单.txt(它确实是工具的一部分),")
        print("  要么把它挪出工具目录(它是我们的东西掉错地方了)。")
    else:
        print("  工具目录里没有多余文件。")
    for p in problems:
        print("  !! %s" % p)
    if not stray and not problems:
        print("  一切正常。")

    if refresh:
        if write_baseline(new_baseline):
            print()
            print("  已重记基线: %s(%d 个工具)" % (baseline_path(),
                                                len(new_baseline)))
        else:
            print()
            print("  !! 基线写不进去: %s" % baseline_path())
    elif not baseline:
        if write_baseline(new_baseline):
            print()
            print("  首次运行, 已记下基线: %s" % baseline_path())

    print()
    print("想让这个项目变干净时, 请从上面那 %.1f KB 里找可删的,"
          % (ours_bytes / 1024.0))
    print("**不要**从 %.1f MB 的工具里找 —— 那儿没有『冗余』, 只有发行版自带的文件。"
          % (tool_bytes / 1048576.0))
    return 1 if (problems or stray) else 0


def inventory_ours(tool_rows):
    """我们自己的文件 = PROJ 下的 .py/.bat + tests + 文档, 排除工具清单里的."""
    tool_paths = []
    for rel, _what in tool_rows:
        full = os.path.normcase(os.path.abspath(
            os.path.join(paths.PROJ, rel.replace("/", os.sep))))
        tool_paths.append(full)
    ours = []
    total = 0
    for base, dirs, files in os.walk(paths.PROJ):
        dirs[:] = [d for d in dirs
                   if d not in ("__pycache__", "videos")]
        for f in files:
            full = os.path.abspath(os.path.join(base, f))
            low = os.path.normcase(full)
            if any(low == t or low.startswith(t + os.sep) for t in tool_paths):
                continue
            if not f.endswith((".py", ".bat", ".md", ".txt", ".json")):
                continue
            if f.endswith(".json") and "tests" not in low:
                continue          # 状态文件/日志不算"我们的代码"
            ours.append(full)
            try:
                total += os.path.getsize(full)
            except OSError:
                pass
    return ours, total


def stray_files(tool_rows):
    """工具目录**同一层**的散落文件(可能是我们掉进去的).

    注意只看"工具目录所在的那些父目录", 不看工具目录里面 ——
    tools/ffmpeg-8.0-full_build/ 里那 44 个文件属于发行版自己, 不是多余文件。
    """
    out = []
    checked = set()
    for rel, _what in tool_rows:
        parent = os.path.dirname(os.path.join(paths.PROJ, rel.replace("/", os.sep)))
        if parent in checked or not os.path.isdir(parent):
            continue
        checked.add(parent)
        try:
            entries = os.listdir(parent)
        except OSError:
            continue
        for name in entries:
            full = os.path.join(parent, name)
            if os.path.isdir(full):
                continue
            rel_to_root = os.path.relpath(full, paths.PROJ).replace(os.sep, "/")
            if not rel_to_root.startswith("tools/"):
                continue
            # 只关心"看起来像我们的东西"(脚本/文档), 别把 BBDown.exe 之类报出来
            if not name.endswith((".py", ".bat", ".md", ".txt")):
                continue
            if name in ("下载设置.txt", "下载设置.示例.txt", "python_paths.txt"):
                continue          # 我们的设置文件/个人文件, 本来就该在这儿
            if name.startswith("工具清单"):
                continue
            out.append(rel_to_root)
    return sorted(out)


if __name__ == "__main__":
    sys.exit(main())
