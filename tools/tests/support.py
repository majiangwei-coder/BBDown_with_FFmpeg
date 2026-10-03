# -*- coding: utf-8 -*-
"""测试用的公共脚手架.

所有测试都在临时目录里跑: 不联网, 不碰真实的 下载状态.json / 守护日志 /
视频目录。这里提供"把整个包指向临时目录"的开关和几个造数据的助手。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

# 真实项目路径 —— 在 import 时抓一次就固定下来.
# 绝对不能用 paths.PROJ 当"真实项目位置": 用例把它重定向到临时目录之后,
# 那个值指向的就是临时目录了(需要读真实文件的用例请用这两个常量)。
REAL_TOOLS = TOOLS
REAL_ROOT = os.path.dirname(TOOLS)

from bbdown_kit import paths, state as state_mod  # noqa: E402


def use_temp_root(testcase):
    """把 paths 指到一个新建的临时目录, 并返回它.

    这是"整个包"的开关: 所有模块都通过 paths 取路径, 所以只要改这里,
    状态文件、日志、锁全都落到临时目录, 真实数据一个字节都不会动。

    PROJ / ROOT 也要一起改 —— 它们是"程序自己的目录", 里面有锁文件、
    "谁在跑"、停止请求这些小文件, 测试里同样不许写到真实项目目录去
    (踩过: 有个用例往真的 tools\\ 里写了一个 守护-停止请求.txt)。
    """
    tmp = tempfile.mkdtemp(prefix="bbdown_test_")
    testcase.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
    testcase._saved_paths = (paths.PROJ, paths.ROOT, paths.DATA_ROOT, paths.LOG_ROOT)
    testcase.addCleanup(_restore_paths, testcase._saved_paths)
    fake_proj = os.path.join(tmp, "tools")
    os.makedirs(fake_proj, exist_ok=True)
    paths.PROJ = fake_proj
    paths.ROOT = tmp
    paths.set_data_root(tmp)
    paths.LOG_ROOT = tmp
    return tmp


def _restore_paths(saved):
    paths.PROJ, paths.ROOT, paths.DATA_ROOT, paths.LOG_ROOT = saved


def write_state(folder, videos, record=None, skip=None, failed=None, meta=None):
    """写一份 下载状态.json(用真实的写入函数, 保证格式一致)."""
    os.makedirs(folder, exist_ok=True)
    st = state_mod.State(folder, videos=list(videos),
                         record=dict(record or {}),
                         skip=dict(skip or {}),
                         failed=dict(failed or {}),
                         meta=dict(meta or {}))
    assert st.save(), "写测试状态文件失败: %s" % folder
    return st


def read_state(folder):
    with open(paths.state_file(folder), encoding="utf-8") as f:
        return json.load(f)


def touch(folder, name, data=b"x"):
    """造一个文件(模拟已下载的视频). data=b"" 就是 0 字节残骸."""
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def rel(*parts):
    """拼出"相对数据目录"的路径写法(分隔符跟系统一致)."""
    return os.sep.join(parts)


def folder(*parts):
    """临时数据目录下的绝对路径."""
    return os.path.join(paths.data_root(), *parts)


class TempRootTest(unittest.TestCase):
    """基类: 每个用例一个干净的临时数据目录.

    连 PROJ/ROOT 也一起重定向, 所以测试绝不会往真实项目目录里写东西。
    需要读真实项目文件(入口脚本、.bat)的用例请用 RealProjectTest。
    """

    def setUp(self):
        self.tmp = use_temp_root(self)

    def up_folder(self, name="123_测试UP"):
        return folder(name)


class RealProjectTest(unittest.TestCase):
    """基类: 只读地检查真实项目文件(入口脚本、.bat、设置).

    这些用例不重定向路径 —— 它们要验证的正是"真实项目里的文件对不对",
    所以只允许读, 不允许写。
    还是给了 self.tmp: 有些用例需要临时放一个假脚本(比如假管理器),
    拿它当落脚点, 不用碰真实目录。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bbdown_real_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def real_path(self, *parts):
        """真实项目里的文件: 在 tools\\ 里找, 找不到再往项目根找一遍."""
        inner = os.path.join(REAL_TOOLS, *parts)
        return inner if os.path.exists(inner) else os.path.join(REAL_ROOT, *parts)

    def read_text(self, *parts):
        with open(self.real_path(*parts), encoding="utf-8",
                  errors="replace") as f:
            return f.read()

    def read_bytes(self, *parts):
        with open(self.real_path(*parts), "rb") as f:
            return f.read()


class FakeResponse(object):
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession(object):
    """假的 requests.Session: 按 url 片段返回预置的 JSON, 不发网络请求.

    routes: [(url片段, 响应 或 [响应, 响应...]), ...]
    一个片段给列表时, 最后一个会一直重复(用来模拟"每次都是这个响应")。
    """

    def __init__(self, routes):
        self.routes = list(routes)
        self.calls = []

    def get(self, url, params=None, timeout=None, **kw):
        self.calls.append((url, dict(params or {})))
        for frag, resp in self.routes:
            if frag in url:
                if isinstance(resp, list):
                    return FakeResponse(resp.pop(0) if len(resp) > 1 else resp[0])
                return FakeResponse(resp)
        raise AssertionError("没有预置这个地址的响应: %s" % url)


def season_page(videos, total, page_size=30, name="某某合集", title="某某合集"):
    """一页合集响应. videos 是 [(aid, title), ...]."""
    return {
        "code": 0,
        "data": {
            "archives": [{"aid": a, "bvid": "BV%s" % a, "title": t}
                         for a, t in videos],
            "meta": {"season_id": 1000001, "name": name, "title": title,
                     "total": total},
            "page": {"page_size": page_size, "total": total},
        },
    }
