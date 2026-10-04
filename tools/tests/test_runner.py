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
