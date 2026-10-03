# -*- coding: utf-8 -*-
"""ASCII 名字的启动器, 真正逻辑在 下载守护.py.

为什么不直接在 .bat 里写 "下载守护.py": cmd.exe 解析含非 ASCII 字节的批处理
文件时会把中文和引号/括号搅在一起(项目里的 find_python.bat 也踩过这个坑),
所以批处理只调用这个纯 ASCII 名字的入口, 中文全部留在 .py 里由 python 处理.

用法和 下载守护.py 完全一样:
    python guard.py --status
    python guard.py            # 补齐/守护
"""

import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "下载守护.py")

if not os.path.exists(TARGET):
    sys.stderr.write("guard.py: cannot find the guard script next to this file\n")
    sys.exit(1)

sys.argv[0] = TARGET
runpy.run_path(TARGET, run_name="__main__")
