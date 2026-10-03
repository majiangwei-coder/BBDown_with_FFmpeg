# -*- coding: utf-8 -*-
"""下载残留 <aid> 临时目录的清理规则.

这个功能会**删用户的文件**, 所以测试的重点不是"能删", 而是"该留的都留住了":
成片、多P目录、别人的文件、正在下的目录 —— 只要有一样对不上就一个字节都不许动。
用例里的文件名/目录结构都是从真实残留里抄来的(见下面每个用例的注释)。
"""

import os
import subprocess
import time
import unittest
from unittest import mock

from support import TempRootTest, folder, touch, write_state

from bbdown_kit import download, workdirs

AID = "100000000000001"
UP = "123456789_某某UP主"


def age_of(path):
    old = time.time() - 3600
    os.utime(path, (old, old))


def make_workdir(folder_, aid, files, age=3600, subdirs=()):
    """造一个 <aid> 临时目录, 里面按 files 落文件, 时间戳往前拨 age 秒."""
    path = os.path.join(folder_, aid)
    os.makedirs(path, exist_ok=True)
    stamp = time.time() - age
    for name in files:
        with open(os.path.join(path, name), "wb") as f:
            f.write(b"x")
        os.utime(os.path.join(path, name), (stamp, stamp))
    for name in subdirs:
        os.makedirs(os.path.join(path, name), exist_ok=True)
        os.utime(os.path.join(path, name), (stamp, stamp))
    os.utime(path, (stamp, stamp))
    return path


# 真实残留里出现过的文件(照抄):
#   <aid>.jpg                        封面
#   <aid>.10000000006.zh-Hans.srt    字幕
#   <aid>.P1.10000000007.mp4         合并好的流
#   00000_<aid>.P4.163099338.vclip   分片(前导 5 位序号)
#   <aid>.aclip
REAL_FILES = [
    "%s.jpg" % AID,
    "%s.10000000006.zh-Hans.srt" % AID,
    "%s.P1.10000000007.mp4" % AID,
    "00000_%s.P4.163099338.vclip" % AID,
    "00001_%s.P4.163099338.vclip" % AID,
    "%s.aclip" % AID,
]


class CleanableRule(TempRootTest):
    """认目录: 四条规则都要满足."""

    def test_real_leftover_is_cleanable(self):
        path = make_workdir(self.up_folder(), AID, REAL_FILES)
        ok, why = workdirs.cleanable(path)
        self.assertTrue(ok, why)

    def test_empty_dir_left_over_by_a_kill_is_cleanable(self):
        """刚建好目录就被掐断: 一个文件都没有, 也算残留."""
        path = make_workdir(self.up_folder(), AID, [])
        self.assertTrue(workdirs.cleanable(path)[0])

    def test_another_aids_file_blocks_the_whole_dir(self):
        """里面有别人的 aid —— 说明这个目录不单纯, 一律不碰."""
        path = make_workdir(self.up_folder(), AID,
                            ["00000_999999999.P1.1.vclip"])
        ok, why = workdirs.cleanable(path)
        self.assertFalse(ok)
        self.assertIn("不属于这个 aid", why)

    def test_file_without_any_aid_blocks(self):
        path = make_workdir(self.up_folder(), AID, ["分片.vclip"])
        self.assertFalse(workdirs.cleanable(path)[0])

    def test_unknown_extension_blocks(self):
        """有个 .txt/.exe 在里面 —— 人放进去的东西, 不能跟着一起删."""
        for name in ("说明.txt", "%s.mp4.exe" % AID):
            path = make_workdir(self.up_folder("2_UP"), AID, [name])
            self.assertFalse(workdirs.cleanable(path)[0], name)

    def test_subdirectory_blocks(self):
        """多P成片就是"目录名_aid\\[P01]xxx.mp4", 有子目录的一律不碰."""
        path = make_workdir(self.up_folder(), AID, REAL_FILES,
                            subdirs=["[P01]上集"])
        ok, why = workdirs.cleanable(path)
        self.assertFalse(ok)
        self.assertIn("子目录", why)

    def test_short_numeric_name_is_not_an_aid(self):
        path = make_workdir(self.up_folder(), "12345", ["%s.vclip" % "12345"])
        self.assertFalse(workdirs.cleanable(path)[0])

    def test_non_numeric_name_is_not_a_workdir(self):
        path = make_workdir(self.up_folder(), "标题_123456", ["x.vclip"])
        self.assertFalse(workdirs.cleanable(path)[0])

    def test_fresh_dir_is_left_alone(self):
        """刚写完的目录 = 可能还在下载, 一个字都不许动."""
        path = make_workdir(self.up_folder(), AID, REAL_FILES, age=0)
        ok, why = workdirs.cleanable(path)
        self.assertFalse(ok)
        self.assertIn("正在下载", why)

    def test_ten_minute_rule(self):
        path = make_workdir(self.up_folder(), AID, REAL_FILES, age=1200)
        self.assertTrue(workdirs.cleanable(path)[0])
        fresh = make_workdir(self.up_folder(), AID, REAL_FILES, age=0)
        self.assertFalse(workdirs.cleanable(fresh)[0])


class ScanAndSweep(TempRootTest):
    """扫描范围: 只扫三类名单文件夹, 只删认定过的."""

    def test_sweep_deletes_only_the_leftover(self):
        up = self.up_folder()
        leftover = make_workdir(up, AID, REAL_FILES)
        # 这些都是必须原样留着的:
        touch(up, "正常视频_222.mp4")                            # 成片
        touch(os.path.join(up, "多P视频_333"), "[P01]上.mp4")     # 多P成片目录
        touch(os.path.join(up, "普通文件夹"), "里面的东西.mp4")     # 人自己建的目录
        fresh = make_workdir(up, "999999999", REAL_FILES, age=0)   # 正在下

        report = workdirs.sweep()
        self.assertEqual(report["cleaned"], 1)
        self.assertGreater(report["freed"], 0)
        self.assertFalse(os.path.exists(leftover))
        self.assertTrue(os.path.exists(fresh), "正在下载的目录被删了")
        for name in ("正常视频_222.mp4",
                     os.path.join("多P视频_333", "[P01]上.mp4"),
                     os.path.join("普通文件夹", "里面的东西.mp4")):
            self.assertTrue(os.path.exists(os.path.join(up, name)), name)
        # 还在下载的那个扫不到(没到 10 分钟), 成片目录名不是纯数字则从来不在范围里
        self.assertEqual(workdirs.scan_folder(up), [])

    def test_dry_run_deletes_nothing(self):
        up = self.up_folder()
        leftover = make_workdir(up, AID, REAL_FILES)
        report = workdirs.sweep(dry_run=True)
        self.assertEqual(report["dirs"], 1)
        self.assertEqual(report["cleaned"], 0)
        self.assertGreater(report["bytes"], 0)
        self.assertTrue(os.path.exists(leftover), "干跑居然删了东西")

    def test_single_video_folder_is_scanned_too(self):
        """单视频下载的数字目录直接躺在名单文件夹里, 也要扫到."""
        single = folder("单视频下载")
        write_state(single, videos=[{"aid": "222", "title": "某视频"}])
        path = make_workdir(single, AID, REAL_FILES)
        report = workdirs.sweep(dry_run=True)
        self.assertEqual([item.name for item in report["items"]], [AID])
        self.assertTrue(os.path.exists(path))

    def test_outside_the_three_folders_is_never_touched(self):
        """数据目录根下随便一个数字目录(可能是人建的)不在扫描范围里."""
        path = make_workdir(folder("随便一个地方"), AID, REAL_FILES)
        self.assertEqual(workdirs.sweep(dry_run=True)["items"], [])
        self.assertTrue(os.path.exists(path))


class RemoveAfterKill(TempRootTest):
    """掐断即清: 刚建好的目录也要清, 但规则和每轮扫描完全一样."""

    def test_fresh_dir_is_cleaned(self):
        path = make_workdir(self.up_folder(), AID, REAL_FILES, age=0)
        freed = workdirs.remove_after_kill(self.up_folder(), AID)
        self.assertGreater(freed, 0)
        self.assertFalse(os.path.exists(path))

    def test_foreign_content_is_left_alone(self):
        up = self.up_folder()
        path = make_workdir(up, AID, ["别人的_888.mp4"], age=0)
        self.assertEqual(workdirs.remove_after_kill(up, AID), 0)
        self.assertTrue(os.path.exists(path))

    def test_missing_dir_is_not_an_error(self):
        self.assertEqual(workdirs.remove_after_kill(self.up_folder(), AID), 0)


class _FakeProc(object):
    """假的 BBDown 进程: 一直在跑, 挨了 taskkill 才死."""

    def __init__(self):
        self.pid = 4242
        self.killed = False

    def wait(self, timeout=None):
        if not self.killed:
            raise subprocess.TimeoutExpired("BBDown.exe", timeout or 0)
        return 0

    def poll(self):
        return 0 if self.killed else None

    def kill(self):
        self.killed = True


class TimeoutCleansUp(TempRootTest):
    """超时被掐掉的那一次: 半成品和临时目录一起收拾干净.

    这是"掐断即清"的落点 —— 不修的话每超时一次就留一个几十 MB~3GB 的目录。
    """

    def test_timeout_kills_and_removes_the_workdir(self):
        up = self.up_folder()
        workdir = make_workdir(up, AID, REAL_FILES, age=0)
        proc = _FakeProc()
        ran = []
        with mock.patch.object(download.subprocess, "Popen", lambda *a, **k: proc), \
                mock.patch.object(download.subprocess, "run",
                                  lambda *a, **k: ran.append(a) or None):
            rc = download.run_bbdown("https://x/av%s" % AID, up, [], timeout=1,
                                     aid=AID)
        self.assertEqual(rc, -9)
        self.assertTrue(proc.killed, "超时了却没把进程收掉")
        self.assertTrue(ran, "没走 taskkill")
        self.assertFalse(os.path.exists(workdir), "临时目录没清掉")

    def test_cleanup_failure_does_not_break_download(self):
        """清理炸了也只是少清一次, 绝不能让下载流程出错."""
        up = self.up_folder()
        with mock.patch.object(workdirs, "remove_after_kill",
                               side_effect=OSError("磁盘炸了")):
            download._clean_workdir(up, AID)      # 不该抛出去

    def test_no_aid_means_no_cleanup(self):
        """老调用点没传 aid: 只收进程, 不碰任何目录."""
        up = self.up_folder()
        workdir = make_workdir(up, AID, REAL_FILES, age=0)
        proc = _FakeProc()
        with mock.patch.object(download.subprocess, "Popen", lambda *a, **k: proc), \
                mock.patch.object(download.subprocess, "run", lambda *a, **k: None):
            rc = download.run_bbdown("https://x/av%s" % AID, up, [], timeout=1)
        self.assertEqual(rc, -9)
        self.assertTrue(os.path.exists(workdir))


class DaemonAndManagerHooks(unittest.TestCase):
    """两个入口都接上了这套清理(不然只有一半的路径会被清)."""

    def _text(self, rel):
        from support import REAL_ROOT
        with open(os.path.join(REAL_ROOT, rel), encoding="utf-8") as f:
            return f.read()

    def test_guard_sweeps_every_round_and_has_a_command(self):
        src = self._text(os.path.join("tools", "下载守护.py"))
        self.assertIn("sweep_workdirs()", src)
        self.assertIn("--clean-workdirs", src)
        # 每轮开跑前扫一遍(在 pending == 0 之前, 待命轮也要扫)
        self.assertLess(src.index("self.sweep_workdirs()"),
                        src.index("if pending == 0:"))

    def test_manager_sweeps_only_when_it_is_the_entry_point(self):
        src = self._text(os.path.join("tools", "BBDown-manager.py"))
        self.assertIn('"--lock-held" not in sys.argv[1:]', src)
        self.assertIn("_sweep_workdirs()", src)


if __name__ == "__main__":
    unittest.main()
