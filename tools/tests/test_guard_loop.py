# -*- coding: utf-8 -*-
"""守护主循环的端到端测试.

为什么需要这个: 守护拆成入口 + 包之后, 每个模块各自都有测试, 但"主循环把
它们串起来"这一段谁都没覆盖 —— 结果就是 run_one_round 返回一个值、调用方
按两个值接, 单测全绿而真跑起来第一轮结束就崩。这里用一个假管理器把主循环
真的跑起来。

假管理器会: 写事件流 + 真的在名单文件夹里造出视频文件, 所以"缺口 -> 0 ->
守护退出"这条主线是真的走通了。
"""

import json
import os
import sys
import unittest
from types import SimpleNamespace

from support import TempRootTest, folder, paths, read_state, write_state

FAKE_MANAGER = '''# -*- coding: utf-8 -*-
"""假下载管理器: 把名单里缺的视频"下"出来, 并写事件流.

数据目录从命令行参数拿(真管理器走的是 paths, 假管理器不 import 包, 所以
直接把路径传进来, 免得自己猜错)。
"""
import json, os, sys

argv = sys.argv[1:]
events = None
data_root = None
for i, a in enumerate(argv):
    if a == "--event-log" and i + 1 < len(argv):
        events = argv[i + 1]
    elif a == "--data-root" and i + 1 < len(argv):
        data_root = argv[i + 1]

DATA = data_root
STATE = "\\u4e0b\\u8f7d\\u72b6\\u6001.json"
VIDEOS = "\\u6295\\u7a3f\\u5217\\u8868"
RECORD = "\\u5df2\\u4e0b\\u8f7d"


def emit(**kw):
    if not events:
        return
    with open(events, "a", encoding="utf-8") as f:
        f.write(json.dumps(kw, ensure_ascii=False) + "\\n")


emit(kind="run_start", pid=os.getpid())
for dirpath, dirnames, filenames in os.walk(DATA):
    if STATE not in filenames:
        continue
    path = os.path.join(dirpath, STATE)
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    videos = data.get(VIDEOS) or []
    record = data.get(RECORD) or {}
    pending = [v for v in videos if v.get("aid") not in record]
    if not pending:
        continue
    print("\\u5904\\u7406UP\\u4e3b: %s" % dirpath)
    emit(kind="up_pending", folder=dirpath, total=len(videos),
         pending=len(pending))
    for v in pending:
        emit(kind="video_start", aid=v["aid"], title=v["title"], folder=dirpath)
        name = "%s_%s.mp4" % (v["title"], v["aid"])
        with open(os.path.join(dirpath, name), "wb") as f:
            f.write(b"fake video")
        record[v["aid"]] = {"title": v["title"], "bvid": v.get("bvid", ""),
                            "time": "fake"}
        data[RECORD] = record
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        emit(kind="video_end", aid=v["aid"], title=v["title"], ok=True,
             rc=0, new_file=True, elapsed=1)
        print("(1/1) \\u5b8c\\u6210 \\u2713 %s" % v["title"])
    emit(kind="up_end", folder=dirpath, total=len(videos),
         downloaded=len(pending), failed=0)
print("\\u5168\\u90e8\\u4efb\\u52a1\\u7ed3\\u675f")
emit(kind="run_end", downloaded=1, failed=0)
'''


class GuardLoopTest(TempRootTest):
    def setUp(self):
        super().setUp()
        self.manager = os.path.join(self.tmp, "fake-manager.py")
        with open(self.manager, "w", encoding="utf-8") as f:
            f.write(FAKE_MANAGER)
        self.run_dir = os.path.join(self.tmp, "logs", "20260101-000000")
        os.makedirs(self.run_dir, exist_ok=True)
        # 守护里有几处一睡就是几分钟到一小时(轮间等待/限流休息), 测试里换掉
        self.guard_module = _load_guard()
        self.slept = []
        self.guard_module.wait_hook = self._no_sleep

    def _no_sleep(self, seconds, note):
        self.slept.append((seconds, note))
        return False                      # False = 没收到停止请求

    def make_guard(self, **over):
        guard = self.guard_module
        args = SimpleNamespace(order=None, no_takeover=True, force=False,
                               max_rounds=5, forever=False)
        for key, value in over.items():
            setattr(args, key, value)
        settings = {"parallel": 1, "interval": 0, "download_timeout_minutes": 30,
                    "order": "collection_first", "data_root": ""}
        g = guard.Guard(args, settings, {}, self.run_dir)
        # 让守护去跑我们的假管理器, 并把数据目录告诉它
        g.manager_command = lambda parallel, interval, events: [
            sys.executable, self.manager, "--data-root", self.tmp,
            "--event-log", events]
        return g

    def test_loop_exits_zero_when_nothing_is_missing(self):
        """没有缺口 -> 主循环应当立刻返回 0(不空跑一轮)."""
        write_state(folder("UP主下载", "123_测试UP"), [])
        guard = self.make_guard()
        self.assertEqual(guard.run(), 0)

    def test_loop_fills_the_gap_and_exits(self):
        """有缺口: 第一轮补齐, 第二轮发现没缺口 -> 返回 0."""
        up = folder("UP主下载", "123_测试UP")
        write_state(up, videos=[{"aid": "1", "bvid": "BV1", "title": "缺的视频"}])
        guard = self.make_guard()
        self.assertEqual(guard.run(), 0)
        # 视频真的被"下"出来了, 记录也写进去了
        self.assertTrue(os.path.exists(os.path.join(up, "缺的视频_1.mp4")))
        self.assertIn("1", read_state(up)["已下载"])
        self.assertEqual(guard.round_no, 2)      # 第一轮补, 第二轮确认已补齐

    def test_ghost_record_is_repaired_by_the_loop(self):
        """有记录但本地没文件 -> 主循环先撤回记录, 再重新下载补齐."""
        up = folder("UP主下载", "123_测试UP")
        write_state(up, videos=[{"aid": "1", "bvid": "BV1", "title": "丢了的视频"}],
                    record={"1": {"title": "丢了的视频", "time": "x"}})
        guard = self.make_guard()
        self.assertEqual(guard.run(), 0)
        self.assertTrue(os.path.exists(os.path.join(up, "丢了的视频_1.mp4")))

    def test_round_logs_and_events_are_written(self):
        """每一轮的人类日志和事件流都要落盘, 出事时才有东西可查."""
        write_state(folder("UP主下载", "123_测试UP"),
                    [{"aid": "1", "bvid": "BV1", "title": "视频"}])
        guard = self.make_guard()
        self.assertEqual(guard.run(), 0)
        names = os.listdir(self.run_dir)
        self.assertIn("第001轮.txt", names)
        self.assertIn("第001轮.events.jsonl", names)
        with open(os.path.join(self.run_dir, "第001轮.events.jsonl"),
                  encoding="utf-8") as f:
            kinds = [json.loads(line)["kind"] for line in f if line.strip()]
        self.assertIn("video_start", kinds)
        self.assertIn("video_end", kinds)
        self.assertIn("run_end", kinds)

    def test_status_file_is_updated(self):
        write_state(folder("UP主下载", "123_测试UP"),
                    [{"aid": "1", "bvid": "BV1", "title": "视频"}])
        guard = self.make_guard()
        guard.run()
        with open(paths.guard_status(), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("已全部补齐", text)

    def test_max_rounds_is_respected(self):
        """假管理器"下"不出文件时不该无限循环: 到轮数上限就返回 1."""
        up = folder("UP主下载", "123_测试UP")
        write_state(up, videos=[{"aid": "1", "bvid": "BV1", "title": "永远下不来"}])
        # 每轮都以"没进展"结束, 缺口一直在
        broken = os.path.join(self.tmp, "broken-manager.py")
        with open(broken, "w", encoding="utf-8") as f:
            f.write("import json, sys\n"
                    "path = sys.argv[sys.argv.index('--event-log') + 1]\n"
                    "open(path, 'a', encoding='utf-8').write("
                    "json.dumps({'kind': 'run_end', 'downloaded': 0,"
                    " 'failed': 0}) + '\\n')\n")
        guard = self.make_guard(max_rounds=2)
        broken_cmd = lambda parallel, interval, events: [           # noqa: E731
            sys.executable, broken, "--event-log", events]
        guard.manager_command = broken_cmd
        self.assertEqual(guard.run(), 1)
        self.assertEqual(guard.round_no, 2)


def _load_guard():
    import importlib.util

    from support import REAL_TOOLS

    path = os.path.join(REAL_TOOLS, "下载守护.py")
    spec = importlib.util.spec_from_file_location("guard_loop_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    unittest.main()
