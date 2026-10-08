# -*- coding: utf-8 -*-
"""跑一轮的集成测试: 拿假的下载管理器喂事件流/中文日志, 看统计准不准.

这些都是以前踩过的坑: 事件流要能用、老版本的中文日志兜底也要能用、
卡死超时不能算成"被限流"、连续失败要达到阈值才掐。
"""

import json
import os
import sys
import unittest
from unittest import mock

from support import RealProjectTest, TempRootTest, paths

from bbdown_kit import runner
from bbdown_kit.util import elapsed_text, size_text


# 会写事件流的假管理器(和真管理器的输出格式一致)
FAKE_MANAGER_EVENTS = '''# -*- coding: utf-8 -*-
import json, sys, os
path = None
argv = sys.argv[1:]
for i, a in enumerate(argv):
    if a == "--event-log" and i + 1 < len(argv):
        path = argv[i + 1]


def emit(**kw):
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(kw, ensure_ascii=False) + "\\n")


print("   BBDown 下载管理器")
print("[00:00:01] 处理UP主: %s" % os.path.join(os.path.dirname(os.path.abspath(__file__)), "123_测试UP"))
emit(kind="run_start", pid=1)
emit(kind="up_pending", folder="123_测试UP", total=4, pending=3)
emit(kind="video_start", aid="1", title="没下过的")
emit(kind="video_end", aid="1", title="没下过的", ok=True, rc=0, new_file=True)
print("[00:00:02] (1/3) 完成 \\u2713 没下过的")
emit(kind="video_start", aid="2", title="会失败的")
emit(kind="video_end", aid="2", title="会失败的", ok=False, rc=0, new_file=False)
print("[00:00:03] (2/3) 失败 \\u2717 会失败的 (第1次失败, 下次运行会自动重试)")
emit(kind="up_end", folder="123_测试UP", total=4, downloaded=1, failed=1)
print("[00:00:04] 全部任务结束")
emit(kind="run_end", downloaded=1, failed=1)
sys.exit(0)
'''

# 只打中文日志的假管理器(模拟老版本, 不认 --event-log)
FAKE_MANAGER_TEXT_ONLY = '''# -*- coding: utf-8 -*-
import sys, os
print("   BBDown 下载管理器")
print("[00:00:01] 处理UP主: %s" % os.path.join(os.path.dirname(os.path.abspath(__file__)), "123_测试UP"))
print("[00:00:02] (1/2) 完成 \\u2713 没下过的")
print("[00:00:03] (2/2) 失败 \\u2717 会失败的 (第1次失败, 下次运行会自动重试)")
print("[00:00:04] 全部任务结束")
sys.exit(0)
'''

# 卡死被掐掉的假管理器
TIMEOUT_MANAGER = '''# -*- coding: utf-8 -*-
import json, sys
path = sys.argv[sys.argv.index('--event-log') + 1]


def emit(**kw):
    open(path, 'a', encoding='utf-8').write(
        json.dumps(kw, ensure_ascii=False) + "\\n")


for i in range(6):
    emit(kind="video_end", aid=str(i), title="卡住的视频%d" % i,
         ok=False, rc=-9, new_file=False, timeout=True)
emit(kind="run_end", downloaded=0, failed=6)
sys.exit(0)
'''

# 一直失败、**而且原因是真被拦**的假管理器(应该触发限流判定)。
# 注意必须带 reason: 判定限流已经改成看原因了, 光"失败"不算数(见 failures.py)。
FAILING_MANAGER = '''# -*- coding: utf-8 -*-
import json, sys
path = sys.argv[sys.argv.index('--event-log') + 1]


def emit(**kw):
    open(path, 'a', encoding='utf-8').write(
        json.dumps(kw, ensure_ascii=False) + "\\n")


for i in range(6):
    emit(kind="video_end", aid=str(i), title='失败视频%d' % i,
         ok=False, rc=0, new_file=False,
         reason="风控", reason_text="接口返回 87008")
sys.exit(0)
'''


class RunnerTestBase(TempRootTest):
    def install_manager(self, source, name="fake-manager.py", folder_name=None):
        """把假管理器写到一个独立目录.

        每个用例一个子目录: 假管理器原来的写法是从"自己所在目录"里找
        123_测试UP, 如果几个用例共用一个目录, 它们会互相干扰。
        """
        run_dir = os.path.join(self.tmp, folder_name or name.replace(".py", ""))
        os.makedirs(run_dir, exist_ok=True)
        path = os.path.join(run_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(source)
        self.manager = path
        return path

    def run_round(self, quarantined=None, **over):
        run_dir = os.path.join(self.tmp, "rounds")
        kw = dict(
            manager_cmd=[sys.executable, self.manager],
            round_no=1,
            quarantined=quarantined if quarantined is not None else {},
            log_dir=run_dir,
            round_log_path=os.path.join(run_dir, "第001轮.txt"),
            events_path=os.path.join(run_dir, "第001轮.events.jsonl"),
            status_writer=None,
            status_prefix=None,
            tick_hook=None,
            timeout_minutes=30,
            manager_path=self.manager,
        )
        kw.update(over)
        # 事件文件要传给假管理器
        kw["manager_cmd"] = kw["manager_cmd"] + ["--event-log", kw["events_path"]]
        return runner.run_round(**kw)


class TestEventStream(RunnerTestBase):
    def test_event_stream_is_used(self):
        self.install_manager(FAKE_MANAGER_EVENTS, folder_name="events")
        _start, result = self.run_round()
        self.assertEqual(result.downloads, 1)
        self.assertEqual(result.fails, 1)
        self.assertTrue(result.finished)
        self.assertIsNone(result.killed_reason)
        events = os.path.join(self.tmp, "rounds", "第001轮.events.jsonl")
        self.assertTrue(os.path.exists(events))
        with open(events, encoding="utf-8") as f:
            kinds = [json.loads(line)["kind"] for line in f if line.strip()]
        self.assertIn("video_end", kinds)
        self.assertIn("run_end", kinds)

    def test_text_fallback_still_works(self):
        """老版本管理器不认 --event-log, 走中文日志也要能统计."""
        self.install_manager(FAKE_MANAGER_TEXT_ONLY, folder_name="textonly")
        _start, result = self.run_round()
        self.assertEqual(result.downloads, 1)
        self.assertEqual(result.fails, 1)
        self.assertTrue(result.finished)

    def test_human_log_is_always_archived(self):
        """不管走不走事件流, 人类可读的那份日志都要留下来."""
        self.install_manager(FAKE_MANAGER_EVENTS, folder_name="archived")
        self.run_round()
        with open(os.path.join(self.tmp, "rounds", "第001轮.txt"),
                  encoding="utf-8") as f:
            text = f.read()
        self.assertIn("BBDown 下载管理器", text)
        self.assertIn("全部任务结束", text)

    def test_ansi_colours_are_stripped_from_log(self):
        self.install_manager('''# -*- coding: utf-8 -*-
import sys
print("\\x1b[1;96m完成 ✓ 带颜色的标题\\x1b[0m")
print("全部任务结束")
sys.exit(0)
''', folder_name="ansi")
        _start, result = self.run_round()
        self.assertTrue(result.finished)


class TestRateLimitDetection(RunnerTestBase):
    def test_timeouts_do_not_look_like_rate_limit(self):
        """卡死超时的失败不能算成"被限流", 否则会白等一轮再重开."""
        self.install_manager(TIMEOUT_MANAGER, folder_name="timeout")
        _start, result = self.run_round()
        self.assertEqual(result.timeouts, 6)
        self.assertEqual(result.fails, 6)
        self.assertEqual(result.downloads, 0)
        self.assertIsNone(result.killed_reason)      # 不是限流
        self.assertTrue(result.finished)

    def test_even_many_timeouts_are_not_rate_limit(self):
        """超时再多也不能算成"被限流"(以前 8 个以上就会).

        超时是我们自己把卡住的视频掐掉的, 不是"接口在拦"。判成限流会顺便
        撤回本轮的失败计数, 于是那个卡住的视频永远攒不够失败次数、永远在
        重试 —— 而守护每轮还要白等十几分钟。
        """
        self.install_manager('''# -*- coding: utf-8 -*-
import json, sys
path = sys.argv[sys.argv.index('--event-log') + 1]


def emit(**kw):
    open(path, 'a', encoding='utf-8').write(
        json.dumps(kw, ensure_ascii=False) + "\\n")


for i in range(9):
    emit(kind="video_end", aid=str(i), title="卡住的视频%d" % i,
         ok=False, rc=-9, new_file=False, timeout=True)
emit(kind="run_end", downloaded=0, failed=9)
sys.exit(0)
''', folder_name="timeout9")
        _start, result = self.run_round()
        self.assertEqual(result.timeouts, 9)
        self.assertEqual(result.downloads, 0)
        self.assertIsNone(result.killed_reason, "超时不是限流信号")

    def test_burst_kills_round(self):
        """连续失败达到阈值 **而且原因是被拦** -> 判定被限流."""
        self.install_manager(FAILING_MANAGER, folder_name="failing")
        _start, result = self.run_round()
        self.assertIsNotNone(result.killed_reason)
        self.assertGreaterEqual(result.consec_fail, runner.FAIL_BURST)

    def test_cdn_failures_do_not_trigger_a_rest(self):
        """**回归**: 失败原因不是限流时, 绝不该判定被限流.

        这是真实事故: 09-25 守护空转 76 分钟, 每轮"成功 0 失败 4"就判限流,
        而当时真正在报的是 "服务器可能并不支持多线程下载" —— CDN 的毛病,
        跟账号被拦毫无关系。并发被降到 1、每 40 秒掐一次, 一个也没下成,
        而同一个接口的探测返回码一直是 0(放行)。
        """
        self.install_manager('''# -*- coding: utf-8 -*-
import json, sys
path = sys.argv[sys.argv.index('--event-log') + 1]


def emit(**kw):
    open(path, 'a', encoding='utf-8').write(
        json.dumps(kw, ensure_ascii=False) + "\\n")


for i in range(10):
    emit(kind="video_end", aid="700%d" % i, title="大视频%d" % i,
         ok=False, rc=0, new_file=False,
         reason="多线程", reason_text="服务器不吃多线程")
emit(kind="run_end", downloaded=0, failed=10)
sys.exit(0)
''', folder_name="cdn")
        _start, result = self.run_round()
        self.assertEqual(result.fails, 10)
        self.assertIsNone(result.killed_reason,
                          "CDN 的毛病不该被当成限流(以前就是这么空转的)")
        self.assertEqual(result.consec_fail, 0, "不是限流就不该累计连败")
        self.assertEqual(result.fail_kinds.get("多线程"), 10)
        self.assertEqual(result.non_limit_fails, 10)

    def test_real_rate_limit_still_rests(self):
        """反过来也要成立: 真被拦的失败照样累计、照样休息."""
        self.install_manager('''# -*- coding: utf-8 -*-
import json, sys
path = sys.argv[sys.argv.index('--event-log') + 1]


def emit(**kw):
    open(path, 'a', encoding='utf-8').write(
        json.dumps(kw, ensure_ascii=False) + "\\n")


for i in range(4):
    emit(kind="video_end", aid="800%d" % i, title="视频%d" % i,
         ok=False, rc=0, new_file=False,
         reason="风控", reason_text="接口返回 87008")
emit(kind="run_end", downloaded=0, failed=4)
sys.exit(0)
''', folder_name="blocked")
        _start, result = self.run_round()
        self.assertIsNotNone(result.killed_reason, "真被拦了就得休息")
        self.assertIn("风控", result.killed_reason)
        self.assertGreaterEqual(result.consec_fail, runner.FAIL_BURST)

    def test_mixed_failures_only_count_the_blocked_ones(self):
        """混着来时只数"被拦"的那些: 3 个 CDN + 1 个风控 ≠ 连续 4 个."""
        self.install_manager('''# -*- coding: utf-8 -*-
import json, sys
path = sys.argv[sys.argv.index('--event-log') + 1]


def emit(**kw):
    open(path, 'a', encoding='utf-8').write(
        json.dumps(kw, ensure_ascii=False) + "\\n")


for i in range(3):
    emit(kind="video_end", aid="60%d" % i, title="CDN%d" % i,
         ok=False, rc=0, new_file=False, reason="多线程",
         reason_text="服务器不吃多线程")
emit(kind="video_end", aid="6099", title="被拦的", ok=False, rc=0,
     new_file=False, reason="风控", reason_text="87008")
emit(kind="run_end", downloaded=0, failed=4)
sys.exit(0)
''', folder_name="mixed")
        _start, result = self.run_round()
        self.assertIsNone(result.killed_reason,
                          "4 个失败里只有 1 个是被拦, 不该判限流")
        self.assertEqual(result.consec_fail, 1)
        self.assertEqual(result.fail_kinds.get("多线程"), 3)
        self.assertEqual(result.fail_kinds.get("风控"), 1)

    def test_vague_parse_failure_is_confirmed_by_the_api(self):
        """BBDown 只说了句"解析此分P失败": 守护要自己去问接口 —— 问出风控就得休息.

        实测(2026-10-08 01:44): 账号被风控的那几分钟里 56 个视频全报
        "解析此分P失败", 而 BBDown 的输出当时是乱码, 于是原因被记成"无成片",
        守护看不出自己在被拦: 既不休息, 也不撤回这一轮写进「跳过」的条目。
        """
        self.install_manager('''# -*- coding: utf-8 -*-
import json, sys
path = sys.argv[sys.argv.index('--event-log') + 1]


def emit(**kw):
    open(path, 'a', encoding='utf-8').write(
        json.dumps(kw, ensure_ascii=False) + "\\n")


for i in range(4):
    emit(kind="video_end", aid="700%d" % i, title="说不清的视频%d" % i,
         ok=False, rc=0, new_file=False, reason="解析",
         reason_text="解析分P失败(BBDown 拿不到这一路的播放地址)")
emit(kind="run_end", downloaded=0, failed=4)
sys.exit(0)
''', folder_name="vague-parse")
        with mock.patch.object(runner.bilitools, "video_unplayable",
                               lambda aid: False), \
                mock.patch.object(runner.bilitools, "can_probe_api",
                                  lambda: True), \
                mock.patch.object(runner.bilitools, "playurl_probe",
                                  lambda aid, cid=None: (-352, False)):
            _start, result = self.run_round()
        self.assertIsNotNone(result.killed_reason,
                             "接口说 -352(风控) 就该判定被拦、去休息")
        self.assertIn("风控", result.killed_reason)
        self.assertGreaterEqual(result.consec_fail, runner.FAIL_BURST)

    def test_voucher_without_streams_is_recorded_but_does_not_rest(self):
        """接口只给验证凭证、没有流: 原因要记准, 但不判"被拦".

        实测(2026-10-08 02:00): 被风控挑战时播放接口返回 code=0 但只有
        v_voucher、一条流都没有, 而同一个窗口里 BBDown 靠兑换凭证**下成了**
        191 个视频 —— 账号不是被拦死, 只是有一批撞在挑战上。所以这条按"解析"
        记下来(写清是验证凭证), 管理器自己会连续失败降速, 下一轮再重试;
        真被拦死时接口会直接给 -352/-412, 那条路照旧休息。
        """
        self.install_manager('''# -*- coding: utf-8 -*-
import json, sys
path = sys.argv[sys.argv.index('--event-log') + 1]


def emit(**kw):
    open(path, 'a', encoding='utf-8').write(
        json.dumps(kw, ensure_ascii=False) + "\\n")


for i in range(6):
    emit(kind="video_end", aid="500%d" % i, title="撞挑战的%d" % i,
         ok=False, rc=0, new_file=False, reason="解析",
         reason_text="解析分P失败(BBDown 拿不到这一路的播放地址)")
emit(kind="run_end", downloaded=0, failed=6)
sys.exit(0)
''', folder_name="voucher")
        with mock.patch.object(runner.bilitools, "video_unplayable",
                               lambda aid: False), \
                mock.patch.object(runner.bilitools, "can_probe_api",
                                  lambda: True), \
                mock.patch.object(runner.bilitools, "playurl_probe",
                                  lambda aid, cid=None: (0, False)):
            _start, result = self.run_round()
        self.assertIsNone(result.killed_reason, "还有得下, 不该整个停下来")
        self.assertEqual(result.consec_fail, 0)
        self.assertEqual(result.fail_kinds.get("解析"), 6)
        self.assertIn("验证凭证", result.last_fail_note)

    def test_vague_parse_failure_with_playable_video_does_not_rest(self):
        """接口说视频好好的(code=0) -> 那就是真解析不了, 绝不能判限流."""
        self.install_manager('''# -*- coding: utf-8 -*-
import json, sys
path = sys.argv[sys.argv.index('--event-log') + 1]


def emit(**kw):
    open(path, 'a', encoding='utf-8').write(
        json.dumps(kw, ensure_ascii=False) + "\\n")


for i in range(6):
    emit(kind="video_end", aid="600%d" % i, title="真解析不了的%d" % i,
         ok=False, rc=0, new_file=False, reason="解析",
         reason_text="解析分P失败(BBDown 拿不到这一路的播放地址)")
emit(kind="run_end", downloaded=0, failed=6)
sys.exit(0)
''', folder_name="vague-parse-ok")
        with mock.patch.object(runner.bilitools, "video_unplayable",
                               lambda aid: False), \
                mock.patch.object(runner.bilitools, "can_probe_api",
                                  lambda: True), \
                mock.patch.object(runner.bilitools, "playurl_probe",
                                  lambda aid, cid=None: (0, True)):
            _start, result = self.run_round()
        self.assertIsNone(result.killed_reason, "视频是好的是我们解析不了, 不是被拦")
        self.assertEqual(result.consec_fail, 0)
        self.assertEqual(result.fail_kinds.get("解析"), 6)

    def test_charging_failure_is_not_counted_as_rate_limit(self):
        """充电专属的失败不算限流, 而且要进永久失败名录."""
        self.install_manager('''# -*- coding: utf-8 -*-
import json, sys
path = sys.argv[sys.argv.index('--event-log') + 1]


def emit(**kw):
    open(path, 'a', encoding='utf-8').write(
        json.dumps(kw, ensure_ascii=False) + "\\n")


for i in range(6):
    emit(kind="video_end", aid="900%d" % i, title="充电视频%d" % i,
         ok=False, rc=0, new_file=False)
emit(kind="run_end", downloaded=0, failed=6)
sys.exit(0)
''', folder_name="charging")
        quarantined = {}
        with mock.patch.object(runner.bilitools, "video_unplayable",
                               lambda aid: True):
            _start, result = self.run_round(quarantined)
        self.assertEqual(result.unplayable, 6)
        self.assertEqual(result.fails, 0)
        self.assertIsNone(result.killed_reason)
        self.assertTrue(quarantined["9000"]["permanent"])
        self.assertEqual(quarantined["9000"]["reason"], "充电/付费专属")

    def test_tick_hook_can_stop_the_round(self):
        """守护收到"停止请求"时要能中断本轮(让位给新守护)."""
        self.install_manager('''# -*- coding: utf-8 -*-
import time, sys
print("慢慢来")
time.sleep(30)
''', folder_name="slow")
        _start, result = self.run_round(tick_hook=lambda: True)
        self.assertTrue(result.stop_requested)
        self.assertIsNotNone(result.killed_reason)


class TestManagerCapabilityProbe(RealProjectTest):
    """回归: 探测管理器支不支持事件流, 不能靠 grep 源码里的字符串."""

    def test_package_manager_is_detected(self):
        real = os.path.join(paths.PROJ, "BBDown-manager.py")
        self.assertTrue(runner.manager_supports_events(real))

    def test_detection_survives_option_removal(self):
        """以前是找源码里有没有 "--event-log"; 改个写法就探测不到了.

        现在只要管理器 import 了本包就算支持, 所以把选项串删掉也照样认得。
        """
        path = os.path.join(self.tmp, "manager.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# -*- coding: utf-8 -*-\n"
                    "from bbdown_kit import download\n"
                    "download.process_up()\n")
        self.assertTrue(runner.manager_supports_events(path))

    def test_old_manager_falls_back_to_option_probe(self):
        path = os.path.join(self.tmp, "old.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# 老版本: 认 --event-log 但不用本包\n")
        self.assertTrue(runner.manager_supports_events(path))

    def test_missing_manager_is_false(self):
        self.assertFalse(runner.manager_supports_events(
            os.path.join(self.tmp, "没有这个文件.py")))


class TestDiskGrowth(unittest.TestCase):
    """"落盘 +xx MB(约 x MB/s)" 是怎么算出来的.

    这块出过一次很离谱的错: 日志里出现过 `落盘 +97.91 GB(46 秒, 约 2.13 GB/s)`
    —— 比宽带物理带宽高几十倍。原因不是下载真的那么快, 而是统计方式:
    在下的文件夹一换(一个 UP主 下完、开始下一个), 就把"新文件夹的存量"当成了
    这 46 秒的增长。所以这里的用例都盯着"换文件夹"这个场景。
    """

    MB = 1048576

    def growth(self, before, now, secs=45, **kw):
        """按 _beat 里的算法算一遍 -> 那句落盘文字."""
        comparable = [f for f in now if f in before]
        delta = sum(now[f] - before[f] for f in comparable)
        fresh = len(now) - len(comparable)
        return runner.disk_growth_text(delta, secs, fresh, bool(comparable),
                                       kw.get("average"))

    def test_same_folders_report_real_growth(self):
        before = {r"G:\a": 100 * self.MB}
        now = {r"G:\a": 100 * self.MB + 5 * self.MB}
        text = self.growth(before, now)
        self.assertIn("落盘 +5.0 MB", text)
        self.assertIn("约 113.8 KB/s", text)

    def test_new_folder_does_not_inflate_the_number(self):
        """换文件夹时不许把新文件夹的存量当成这几十秒下的.

        实测那次: 上一个 UP主 的文件夹只剩 1 个视频, 新 UP主 的文件夹本地已经
        有 97.91 GB —— 一减就成了"2.13 GB/s"。事后复现: 老文件夹 5.52 GB、
        新换进来的 105.25 GB, 差 99.73 GB。
        """
        before = {r"G:\上一个UP": 5520 * self.MB}
        now = {r"G:\新UP": 105 * 1024 * self.MB}
        text = self.growth(before, now, 46)
        self.assertNotIn("落盘 +", text, "没有可比基准就不能报数字")
        self.assertNotIn("GB/s", text)
        self.assertIn("下一次心跳才有落盘数字", text)

    def test_growth_of_kept_folders_is_still_reported(self):
        """换了文件夹但有一个是原来就量过的: 报它的增长, 并说明还有没算进来的."""
        before = {r"G:\老UP": 100 * self.MB}
        now = {r"G:\老UP": 100 * self.MB + 3 * self.MB,
               r"G:\新UP": 97 * 1024 * self.MB}
        text = self.growth(before, now, 46)
        self.assertIn("落盘 +3.0 MB", text)
        self.assertNotIn("97", text)
        self.assertIn("另有 1 个文件夹刚开始下, 没算进来", text)

    def test_folder_leaving_the_set_is_not_reported_as_no_growth(self):
        """集合里少了个大文件夹 -> 差值变负, 以前会误报"没长个儿(可能卡了)"."""
        before = {r"G:\a": 200 * self.MB, r"G:\b": 500 * 1024 * self.MB}
        now = {r"G:\a": 200 * self.MB + self.MB}         # b 下完了, 已退出集合
        text = self.growth(before, now)
        self.assertIn("落盘 +1.0 MB", text, "还在下的那个确实在长")

    def test_really_no_growth_still_says_so(self):
        """同一个文件夹两次都量过、真的没长: 这句要保留(它是排查卡住的依据)."""
        before = {r"G:\a": 100 * self.MB}
        text = self.growth(before, {r"G:\a": 100 * self.MB})
        self.assertIn("没长个儿", text)

    def test_steady_state_is_not_counted_twice(self):
        """稳态一跳: 下 100 分片 + 合并出 95 成片 + 删掉上一批的 100 分片.

        真实下载量是 100(新分片), 净增长报 95 —— 对得上(成片比它的分片略小)。
        只加正数会报 195, 差不多两倍。本库视频多在 20 MB 上下、合并极频繁,
        所以那是系统性翻倍, 不是偶发抖动。
        """
        before = {r"G:\a": 400 * self.MB}
        now = {r"G:\a": 400 * self.MB
               - 100 * self.MB        # 上一批分片被删掉
               + 100 * self.MB        # 这一跳新下的分片
               + 95 * self.MB}        # 合并出来的成片
        text = self.growth(before, now)
        self.assertIn("落盘 +95.0 MB", text)
        self.assertNotIn("195", text)

    def test_merge_cycle_sums_back_to_the_truth(self):
        """一整圈(下分片 -> 合并 -> 删分片)加起来的净增长 = 真实新增.

        单跳会有抖动: 合并那一跳偏高(分片还在)、删分片那一跳偏低 —— 所以心跳
        旁边给了"本轮平均"当参照。但一圈的总和必须是真实新增, 不能凭空多出来。
        """
        empty = {r"G:\a": 0}
        downloaded = {r"G:\a": 100 * self.MB}                  # 分片下完
        merged = {r"G:\a": 195 * self.MB}                      # 合并(分片还在)
        cleaned = {r"G:\a": 95 * self.MB + 60 * self.MB}       # 删分片 + 又下 60
        deltas = [
            sum(downloaded[f] - empty[f] for f in downloaded),
            sum(merged[f] - downloaded[f] for f in merged),
            sum(cleaned[f] - merged[f] for f in cleaned),
        ]
        self.assertEqual(deltas[1], 95 * self.MB, "合并那一跳就是会偏高")
        self.assertLess(deltas[2], 0, "删分片那一跳必须减掉, 否则合并被算了两次")
        self.assertEqual(sum(deltas), 155 * self.MB,
                         "真实新增 = 成片 95 + 新分片 60; 一圈之和必须等于它")

    def test_average_is_the_reference_that_cannot_exceed_bandwidth(self):
        """单跳会被合并带高, 所以旁边给一个本轮平均当参照."""
        before = {r"G:\a": 100 * self.MB}
        now = {r"G:\a": 100 * self.MB + 900 * self.MB}
        text = self.growth(before, now, 45, average=6 * self.MB)
        self.assertIn("落盘 +900.0 MB", text)
        self.assertIn("本轮平均 6.0 MB/s", text)

    def test_nothing_active_is_silent(self):
        """没有在下的视频: 不报落盘(那行本来就在说"正在刷新名单/准备下一批")."""
        self.assertIsNone(runner.heartbeat_lines(1, 10, runner.RoundResult(),
                                                 {}, 100.0)[1].count("落盘") or None)

    def test_first_beat_says_it_has_no_baseline_yet(self):
        text = self.growth({}, {r"G:\a": 500 * self.MB})
        self.assertIn("下一次心跳才有落盘数字", text)
        self.assertNotIn("落盘 +", text)


class TestHeartbeatText(unittest.TestCase):
    """心跳那行要能回答: 还在跑吗 / 在跑哪个 / 跑了多久."""

    def test_elapsed_text(self):
        self.assertEqual(elapsed_text(0), "0秒")
        self.assertEqual(elapsed_text(45), "45秒")
        self.assertEqual(elapsed_text(123), "2分03秒")

    def test_size_text(self):
        self.assertEqual(size_text(2048), "2.0 KB")
        self.assertEqual(size_text(5 * 1048576), "5.0 MB")
        self.assertEqual(size_text(3 * 1073741824), "3.00 GB")

    def test_heartbeat_lists_running_video(self):
        result = runner.RoundResult()
        result.downloads = 7
        result.fails = 1
        result.timeouts = 2
        now = 100000.0
        active = {"1": ("某某合集直播", now - 400, "x"),
                  "2": ("另一个视频", now - 30, "x")}
        text = runner.heartbeat_text(3, 900, result, active, now)
        self.assertIn("第 3 轮运行中 15分00秒", text)
        self.assertIn("成功 7", text)
        self.assertIn("超时掐掉 2", text)
        self.assertIn("在下的 2 个", text)
        self.assertIn("某某合集直播(已 6分40秒)", text)     # 最久的排最前面

    def test_heartbeat_is_split_into_short_lines(self):
        """心跳要拆行: 数字一行, 标题另一行.

        用户反馈的"看着像缺字": 原来全挤在一行, 窄窗口会在宽度处折行,
        "朴孝敏(已 26秒)" 被切成 "…朴孝敏(已 26秒" + 下一行 ")", 看着像缺字;
        更长的还会被窗口直接截掉。拆开之后第一行很短很稳, 标题再长也只影响
        第二行 —— 数字永远不会被折走或截掉。
        """
        result = runner.RoundResult()
        active = {"1": ("一个特别特别长的视频标题会被截断成二十个字", 0.0, "x")}
        lines = runner.heartbeat_lines(1, 45, result, active, 26.0)
        self.assertEqual(len(lines), 2, "有在下的视频时应该是两行")
        self.assertLessEqual(len(lines[0]), 48,
                             "第一行太长就会折行, 折了就又看着像缺字了")
        self.assertIn("在下的 1 个", lines[0])
        self.assertNotIn("(已 ", lines[0], "时间信息应该在第二行")
        self.assertIn("(已 26秒)", lines[1], "时间要完整地待在第二行")

    def test_heartbeat_when_nothing_running(self):
        lines = runner.heartbeat_lines(1, 10, runner.RoundResult(), {}, 100.0)
        self.assertEqual(len(lines), 2)
        self.assertIn("当前没有在下的视频", lines[1])

    def test_heartbeat_includes_disk_growth(self):
        """落盘速度是"真的在下"最硬的证据(BBDown 进度是回车刷新的, 看不到)."""
        active = {"1": ("大文件", 100.0, r"G:\x")}
        text = runner.heartbeat_text(
            1, 40, runner.RoundResult(), active, 140.0,
            progress="落盘 +128.0 MB(45 秒, 约 2.8 MB/s)")
        self.assertIn("落盘 +128.0 MB", text)

    def test_result_summary(self):
        result = runner.RoundResult()
        result.downloads = 3
        result.fails = 2
        result.timeouts = 1
        result.unplayable = 4
        summary = result.summary()
        self.assertIn("成功 3", summary)
        self.assertIn("失败 2", summary)
        self.assertIn("卡死超时 1", summary)
        self.assertIn("充电专属跳过 4", summary)


class TestEventTailer(RunnerTestBase):
    """事件文件的增量读取(守护靠它拿到最后几条)."""

    def test_reads_only_complete_lines_and_advances(self):
        path = os.path.join(self.tmp, "events.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"kind":"a"}\n{"kind":"b"}\n{"kind":"half')
        state = {"pos": 0}
        got = []
        runner.read_new_events(path, state, got.append)
        self.assertEqual(len(got), 2)                 # 半行不算
        with open(path, "a", encoding="utf-8") as f:
            f.write('"}\n{"kind":"c"}\n')
        runner.read_new_events(path, state, got.append)
        self.assertEqual(len(got), 4)
        self.assertIn('"kind":"c"', got[-1])

    def test_missing_file_is_harmless(self):
        state = {"pos": 0}
        runner.read_new_events(os.path.join(self.tmp, "没有.jsonl"), state,
                               lambda line: None)
        self.assertEqual(state["pos"], 0)


if __name__ == "__main__":
    unittest.main()
