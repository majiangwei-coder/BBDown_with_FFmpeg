# -*- coding: utf-8 -*-
"""失败分类 + 原因感知的限流判定.

这一整套是被一次真实事故逼出来的: 守护判"被限流"只看"连续失败 4 个",
不看原因。09-25 实测空转 76 分钟, 每轮"成功 0 失败 4"就判限流, 而真正在报的是
    "服务器可能并不支持多线程下载, 请使用 --multi-thread false 关闭多线程"
—— CDN 的毛病, 跟账号被拦毫无关系; 而同一个接口的探测返回码一直是 0。
"""

import os
import unittest
from unittest import mock

from support import TempRootTest, folder, read_state, write_state

from bbdown_kit import failures


class TestClassify(unittest.TestCase):
    """BBDown 的真实报错 -> 分类. 用的都是实测日志里的原话."""

    def test_multithread_is_not_a_rate_limit(self):
        kind, note = failures.classify(
            "[2026-10-02 13:50:05] - 服务器可能并不支持多线程下载, "
            "请使用 --multi-thread false 关闭多线程")
        self.assertEqual(kind, failures.MULTITHREAD)
        self.assertFalse(failures.should_rest(kind),
                         "服务器不吃多线程不是限流, 不该让守护休息")
        self.assertIn("多线程", note)

    def test_generic_retry_is_network(self):
        kind, _ = failures.classify("下载出现异常, 3秒后将进行自动重试...")
        self.assertEqual(kind, failures.NETWORK)
        self.assertFalse(failures.should_rest(kind))

    def test_clip_truncation(self):
        kind, _ = failures.classify("Failed to download clip 109")
        self.assertEqual(kind, failures.CLIP)
        self.assertFalse(failures.should_rest(kind))

    def test_parse_failure(self):
        for text in ("解析此分P失败(建议--debug查看详细信息)",
                     "请尝试升级到最新版本后重试!"):
            kind, _ = failures.classify(text)
            self.assertEqual(kind, failures.PARSE, text)

    def test_rate_limit_codes_do_rest(self):
        for text in ("返回码=-799", "接口返回 87008", "请求过于频繁, 请稍后再试",
                     "账号异常, 拒绝访问"):
            kind, _ = failures.classify(text)
            self.assertTrue(failures.should_rest(kind),
                            "%r 应该让守护休息" % text)

    def test_non_rate_limit_code_is_not_a_rest(self):
        """-10403(充电专属)/-404(视频没了) 不是限流."""
        for text in ("返回码=-10403", "返回码=-404"):
            kind, _ = failures.classify(text)
            self.assertFalse(failures.should_rest(kind), text)

    def test_unrecognised_is_unknown_and_never_rests(self):
        kind, _ = failures.classify("一切正常, 下载完毕")
        self.assertEqual(kind, failures.UNKNOWN)
        self.assertFalse(failures.should_rest(kind),
                         "认不出来就绝不能当成限流")

    def test_empty_input(self):
        self.assertEqual(failures.classify(""), (failures.UNKNOWN, ""))
        self.assertEqual(failures.classify(None), (failures.UNKNOWN, ""))

    def test_multithread_wins_over_the_generic_retry_line(self):
        """两句话常常同现: 具体的(多线程)要盖过笼统的(下载出现异常)."""
        text = ("服务器可能并不支持多线程下载, "
                "请使用 --multi-thread false 关闭多线程\n"
                "下载出现异常, 3秒后将进行自动重试...")
        kind, _ = failures.classify(text)
        self.assertEqual(kind, failures.MULTITHREAD)

    def test_scan_is_line_order_independent_for_rest_causes(self):
        """整段里只要出现过"真被拦", 结论就该是"该休息"."""
        text = ("下载出现异常, 3秒后将进行自动重试...\n"
                "服务器可能并不支持多线程下载\n"
                "返回码=-799\n")
        kind, _ = failures.scan(text)
        self.assertTrue(failures.should_rest(kind))

    def test_scan_of_only_cdn_noise_is_not_a_rest(self):
        text = ("服务器可能并不支持多线程下载\n"
                "下载出现异常, 3秒后将进行自动重试...\n" * 5)
        kind, _ = failures.scan(text)
        self.assertFalse(failures.should_rest(kind))

    def test_every_kind_has_a_readable_name(self):
        for kind in (failures.RATE_LIMIT, failures.THROTTLED,
                     failures.MULTITHREAD, failures.NETWORK, failures.PARSE,
                     failures.CLIP, failures.UNKNOWN):
            self.assertTrue(kind)
            self.assertIsInstance(failures.advice(kind), str)
        # 只有这两种该休息, 多一个少一个都要重新想清楚
        self.assertEqual(failures.REST_CAUSES,
                         frozenset((failures.RATE_LIMIT, failures.THROTTLED)))


class TestTailCapturesOutput(unittest.TestCase):
    """截获 BBDown 输出的那个尾巴: 要转发, 也要留住最后几行."""

    def test_forwards_and_keeps_only_the_tail(self):
        from bbdown_kit import download
        import io
        out = io.StringIO()
        tail = download._Tail(stream=out)
        for i in range(200):
            tail.feed("第 %d 行\n" % i)
        self.assertEqual(len(tail.lines), download._Tail.MAX_LINES,
                         "只该留最后几行, 不然几万行进度会把内存吃掉")
        self.assertIn("第 199 行", tail.text())
        self.assertNotIn("第 0 行", tail.text())
        self.assertIn("第 100 行", out.getvalue(), "必须原样转发给控制台")

    def test_reason_from_captured_output(self):
        from bbdown_kit import download
        tail = download._Tail(stream=None)
        tail.feed("开始下载P1视频...\n")
        tail.feed("服务器可能并不支持多线程下载, "
                  "请使用 --multi-thread false 关闭多线程\n")
        tail.feed("下载出现异常, 3秒后将进行自动重试...\n")
        kind, _note = tail.reason()
        self.assertEqual(kind, failures.MULTITHREAD)

    def test_a_broken_console_cannot_break_downloads(self):
        from bbdown_kit import download

        class Boom(object):
            def write(self, _s):
                raise IOError("控制台没了")

            def flush(self):
                raise IOError("控制台没了")

        tail = download._Tail(stream=Boom())
        tail.feed("随便什么\n")          # 不该抛
        self.assertIn("随便什么", tail.text())


class _FakePipe(object):
    """冒充 BBDown 的 stdout 管道(字节, 有 read/close)."""

    def __init__(self, data):
        self.data = data

    def read(self, n=-1):
        chunk, self.data = self.data[:n], self.data[n:]
        return chunk

    def close(self):
        pass


class TestTailDecodesBBDownOutput(unittest.TestCase):
    """BBDown 的输出编码**不固定**, 必须逐行认 —— 写死 utf-8 会把中文全毁掉.

    实测(2026-10-08 01:44): cp936 的控制台里 BBDown 写的是 GBK, 而这里以前是
    `encoding="utf-8", errors="replace"` —— 一轮日志里 **6 万个 U+FFFD**, 中文
    一个字都没剩下。后果不只是日志难看: 失败原因也认不出来, 56 个"解析此分P失败"
    全被记成"无成片", 守护因此看不出自己正被风控。
    """

    def test_gbk_lines_are_decoded(self):
        from bbdown_kit import download
        raw = "解析此分P失败(开启--debug查看详细信息)\n任务完成\n".encode("gb18030")
        lines, tail = download.split_output(raw)
        self.assertEqual(tail, b"", "整段都以换行结束, 不该留尾巴")
        text = "".join(lines)
        self.assertIn("解析此分P失败", text)
        self.assertIn("任务完成", text)
        self.assertNotIn("\ufffd", text)

    def test_utf8_lines_are_decoded_too(self):
        """chcp 65001 的窗口里 BBDown 写 UTF-8, 这条也不能挂."""
        from bbdown_kit import download
        raw = "解析此分P失败\n任务完成\n".encode("utf-8")
        lines, _tail = download.split_output(raw)
        self.assertIn("解析此分P失败", "".join(lines))

    def test_progress_refreshed_with_carriage_return_is_split(self):
        """进度是 \\r 刷新的: 不切成一行行, 一条进度条会攒成一大坨."""
        from bbdown_kit import download
        raw = "下载中 1%\r下载中 2%\r下载中 3%\n".encode("gb18030")
        lines, tail = download.split_output(raw)
        self.assertEqual(tail, b"")
        self.assertEqual(len(lines), 3, "回车刷新也要断行")
        self.assertIn("下载中 3%", lines[-1])

    def test_crlf_is_one_break_not_two(self):
        from bbdown_kit import download
        lines, _tail = download.split_output("第一行\r\n第二行\r\n".encode("utf-8"))
        self.assertEqual(lines, ["第一行", "第二行"])

    def test_incomplete_tail_is_kept_for_the_next_chunk(self):
        """半个汉字/半行留在尾巴里, 拼上下一段再解 —— 不能丢字."""
        from bbdown_kit import download
        whole = "解析此分P失败".encode("gb18030")
        lines, tail = download.split_output(whole[:5])
        self.assertEqual(lines, [])
        self.assertTrue(tail)
        lines2, tail2 = download.split_output(tail + whole[5:] + b"\n")
        self.assertEqual(tail2, b"")
        self.assertEqual("".join(lines2), "解析此分P失败")

    def test_tail_still_classifies_the_reason_after_decoding(self):
        """整条链路: GBK 的"解析此分P失败"喂进去, reason() 要认出"解析".

        以前这里会得到"无成片"(因为中文全成了乱码), 于是"是不是被风控"就看不出来了。
        """
        from bbdown_kit import download
        tail = download._Tail(stream=None)
        tail.start(_FakePipe(
            "开始解析P1...\n解析此分P失败(开启--debug查看详细信息)\n任务完成\n"
            .encode("gb18030")))
        tail.join()
        self.assertIn("解析此分P失败", tail.text())
        self.assertNotIn("\ufffd", tail.text())
        self.assertEqual(tail.reason()[0], failures.PARSE)

    def test_text_pipe_still_works(self):
        """万一传进来的是文本管道(替身/老调用点), 也不能崩."""
        from bbdown_kit import download
        tail = download._Tail(stream=None)
        tail.start(_FakePipe("普通文本\n".encode("utf-8")))
        tail.join()
        self.assertIn("普通文本", tail.text())


class TestPlayurlProbe(unittest.TestCase):
    """播放接口的"有没有流"必须单独看 —— 返回码 0 也可能是被风控挑战.

    实测(2026-10-08 02:00): 连问四个视频, 全是 code=0 但 data 里只有一个
    v_voucher、没有 dash/durl; 而同一时间 BBDown 正在报"解析此分P失败"。
    只看返回码的探测会把这种状态读成"已放行"(守护那句日志就是这么骗人的)。
    """

    def probe(self, payload, code=0):
        from bbdown_kit import bilitools
        with mock.patch.object(bilitools, "api_session",
                               lambda: _FakeSession({"code": code, "data": payload})), \
                mock.patch.object(bilitools, "mixin_key", lambda session: "k"):
            return bilitools.playurl_probe("1", cid="2")

    def test_voucher_only_is_no_stream(self):
        self.assertEqual(self.probe({"v_voucher": "voucher_x"}), (0, False))

    def test_dash_streams_count_as_stream(self):
        self.assertEqual(
            self.probe({"dash": {"video": [{"baseUrl": "http://x"}]}}), (0, True))

    def test_durl_counts_as_stream(self):
        self.assertEqual(self.probe({"durl": [{"url": "http://x"}]}), (0, True))

    def test_error_code_is_passed_through(self):
        self.assertEqual(self.probe({}, code=-352), (-352, False))

    def test_playurl_ok_needs_streams_not_just_code_zero(self):
        from bbdown_kit import bilitools
        with mock.patch.object(bilitools, "api_session",
                               lambda: _FakeSession({"code": 0, "data": {"v_voucher": "v"}})), \
                mock.patch.object(bilitools, "mixin_key", lambda session: "k"):
            self.assertFalse(bilitools.playurl_ok("1", cid="2"),
                             "只给验证凭证 = 现在下不了, 不能说它 ok")
        with mock.patch.object(bilitools, "api_session",
                               lambda: _FakeSession(
                                   {"code": 0, "data": {"durl": [{"url": "u"}]}})), \
                mock.patch.object(bilitools, "mixin_key", lambda session: "k"):
            self.assertTrue(bilitools.playurl_ok("1", cid="2"))


class _FakeSession(object):
    """只认一次 playurl 请求的假会话."""

    def __init__(self, payload):
        self.payload = payload

    def get(self, url, params=None, timeout=None, **kw):
        from support import FakeResponse
        return FakeResponse(self.payload)


class TestCodeKind(unittest.TestCase):
    """接口返回码 -> 失败分类: BBDown 说不清时, 我们自己问接口要这个码."""

    def test_rate_limit_codes(self):
        for code in (-352, -412, -799, -509, -401, 87008):
            kind, why = failures.code_kind(code)
            self.assertEqual(kind, failures.RATE_LIMIT, code)
            self.assertTrue(why)
            self.assertTrue(failures.should_rest(kind), code)

    def test_playable_and_unknown_codes_are_not_a_reason(self):
        self.assertIsNone(failures.code_kind(0), "code 0 = 正常返回, 不是失败")
        self.assertIsNone(failures.code_kind(None), "没探到就当没证据")

    def test_parse_codes_stay_parse(self):
        for code in (-404, -10403):
            kind, _why = failures.code_kind(code)
            self.assertEqual(kind, failures.PARSE, code)
            self.assertFalse(failures.should_rest(kind), code)


class TestStreamVerdict(unittest.TestCase):
    """(返回码, 有没有流) -> 分类. 这是"BBDown 说不清时我们自己问接口"的翻译层."""

    def test_voucher_without_streams_is_a_parse_failure_with_a_clear_note(self):
        kind, why = failures.stream_verdict(0, False)
        self.assertEqual(kind, failures.PARSE)
        self.assertIn("验证凭证", why)
        self.assertFalse(failures.should_rest(kind),
                         "同一窗口里 BBDown 还能下成 191 个, 不该整个停下来")

    def test_code_zero_with_streams_is_not_a_reason(self):
        self.assertIsNone(failures.stream_verdict(0, True),
                          "接口现在能给流 -> 上次失败是瞬时的, 不编原因")

    def test_nothing_probed_is_not_a_reason(self):
        self.assertIsNone(failures.stream_verdict(None, None))

    def test_rate_limit_codes_still_rest(self):
        kind, why = failures.stream_verdict(-352, False)
        self.assertEqual(kind, failures.RATE_LIMIT)
        self.assertTrue(failures.should_rest(kind))
        self.assertIn("风控", why)


class TestFailureReasonIsRecorded(TempRootTest):
    """（D）失败必须记下原因 —— 以前只记 count/time, 事后完全查不出为什么."""

    def test_mark_failed_records_the_reason(self):
        from bbdown_kit import state as state_mod
        up = folder("UP主下载", "123_测试UP")
        write_state(up, videos=[{"aid": "1", "bvid": "", "title": "t"}])
        st = state_mod.State.load(up)
        st.mark_failed("1", "t", when="2026-10-02 10:00:00",
                       reason="多线程: 服务器不吃多线程")
        st.save()
        data = read_state(up)
        entry = data["失败"]["1"]
        self.assertEqual(entry["原因"], "多线程: 服务器不吃多线程")
        self.assertEqual(entry["count"], 1)

    def test_reason_survives_the_move_to_skip(self):
        from bbdown_kit import state as state_mod
        up = folder("UP主下载", "123_测试UP")
        write_state(up, videos=[{"aid": "1", "bvid": "", "title": "t"}])
        st = state_mod.State.load(up)
        st.mark_failed("1", "t", reason="解析: 解析播放地址失败")
        st.move_failed_to_skip("1")
        st.save()
        data = read_state(up)
        self.assertEqual(data["跳过"]["1"]["原因"], "解析: 解析播放地址失败",
                         "跳过的条目也要能看出当初为什么被跳过")

    def test_mark_failed_without_reason_still_works(self):
        """旧调用点不传原因也不能坏."""
        from bbdown_kit import state as state_mod
        up = folder("UP主下载", "123_测试UP")
        write_state(up, videos=[{"aid": "1", "bvid": "", "title": "t"}])
        st = state_mod.State.load(up)
        self.assertEqual(st.mark_failed("1", "t"), 1)
        self.assertNotIn("原因", st.failed["1"])


class TestRoundSummaryShowsReasons(unittest.TestCase):
    def test_summary_lists_the_reason_mix(self):
        from bbdown_kit import runner
        result = runner.RoundResult()
        result.downloads = 1
        result.fails = 5
        result.fail_kinds = {"多线程": 4, "网络": 1}
        text = result.summary()
        self.assertIn("多线程 4", text)
        self.assertIn("网络 1", text)


class TestProbeUsesARealDownload(unittest.TestCase):
    """（C）探测不能只看接口返回码 —— 那正是空转 76 分钟的成因."""

    def test_download_path_ok_reports_a_refusal(self):
        from bbdown_kit import bilitools
        session = mock.Mock()
        resp = mock.Mock()
        resp.status_code = 403
        resp.iter_content.return_value = []
        session.get.return_value = resp
        with mock.patch.object(bilitools, "api_session", lambda: session), \
                mock.patch.object(bilitools, "playurl_media_url",
                                  lambda aid, cid=None: "http://cdn/x.mp4"):
            ok, why = bilitools.download_path_ok("1")
        self.assertFalse(ok)
        self.assertIn("403", why)
        self.assertIn("风控", why)

    def test_download_path_ok_reports_success_with_byte_count(self):
        from bbdown_kit import bilitools
        session = mock.Mock()
        resp = mock.Mock()
        resp.status_code = 206
        resp.iter_content.return_value = [b"x" * 70000, b"y" * 70000]
        session.get.return_value = resp
        with mock.patch.object(bilitools, "api_session", lambda: session), \
                mock.patch.object(bilitools, "playurl_media_url",
                                  lambda aid, cid=None: "http://cdn/x.mp4"):
            ok, why = bilitools.download_path_ok("1")
        self.assertTrue(ok)
        self.assertIn("字节", why)

    def test_no_media_url_means_not_ok(self):
        from bbdown_kit import bilitools
        with mock.patch.object(bilitools, "api_session", lambda: mock.Mock()), \
                mock.patch.object(bilitools, "playurl_media_url",
                                  lambda aid, cid=None: None), \
                mock.patch.object(bilitools, "playurl_probe",
                                  lambda aid, cid=None: (87008, False)):
            ok, why = bilitools.download_path_ok("1")
        self.assertFalse(ok)
        self.assertIn("87008", why)

    def test_voucher_only_says_risk_control_not_ok(self):
        """拿不到地址 + 返回码 0: 别说成"接口正常", 要说清是只给了验证凭证."""
        from bbdown_kit import bilitools
        with mock.patch.object(bilitools, "api_session", lambda: mock.Mock()), \
                mock.patch.object(bilitools, "playurl_media_url",
                                  lambda aid, cid=None: None), \
                mock.patch.object(bilitools, "playurl_probe",
                                  lambda aid, cid=None: (0, False)):
            ok, why = bilitools.download_path_ok("1")
        self.assertFalse(ok)
        self.assertIn("验证凭证", why)

    def test_media_url_accepts_both_dash_and_durl(self):
        """实测这个接口按参数不同会返回 dash 或 durl, 两种都要认."""
        from bbdown_kit import bilitools
        session = mock.Mock()
        session.get.return_value = FakeJson({
            "code": 0,
            "data": {"durl": [{"url": "http://cdn/durl.mp4"}]},
        })
        with mock.patch.object(bilitools, "api_session", lambda: session), \
                mock.patch.object(bilitools, "mixin_key", lambda s: "k"), \
                mock.patch.object(bilitools, "wbi_sign", lambda p, k: p):
            self.assertEqual(bilitools.playurl_media_url("1", cid="2"),
                             "http://cdn/durl.mp4")

        session.get.return_value = FakeJson({
            "code": 0,
            "data": {"dash": {"video": [{"baseUrl": "http://cdn/dash.m4s"}]}},
        })
        with mock.patch.object(bilitools, "api_session", lambda: session), \
                mock.patch.object(bilitools, "mixin_key", lambda s: "k"), \
                mock.patch.object(bilitools, "wbi_sign", lambda p, k: p):
            self.assertEqual(bilitools.playurl_media_url("1", cid="2"),
                             "http://cdn/dash.m4s")


class FakeJson(object):
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class TestSignalCaptureSurvivesTheWindow(unittest.TestCase):
    """原因必须"到达即记住", 不能只看最后 N 行.

    两个真实成因:
      · 同一个文件夹并行下载时, 几个 BBDown 共用一个 stdout 管道, 输出会交错,
        固定窗口里很可能全是别人的输出;
      · 原因不一定在最后 —— 实测 "解析此分P失败" 之后还会打印几十行
        "清理临时文件/任务完成", 原因被顶出窗口就再也找不回来。
    """

    def _tail(self, stream=None):
        from bbdown_kit import download
        return download._Tail(stream=stream)

    def test_reason_found_when_it_scrolled_far_away(self):
        from bbdown_kit import download
        t = self._tail()
        t.feed("[2026-10-02 20:38:25.057] - 解析此分P失败(建议--debug查看详细信息)\n")
        # 后面再灌 300 行(远超 MAX_LINES), 把原因顶出窗口
        for i in range(300):
            t.feed("清理临时文件... 第 %d 行\n" % i)
        self.assertNotIn("解析此分P失败", t.text(),
                         "前提: 原因确实已经被顶出保留窗口了")
        kind, note = t.reason()
        self.assertEqual(kind, failures.PARSE, "顶出窗口也必须还认得出来")
        self.assertIn("分P", note)

    def test_reason_amid_interleaved_output(self):
        """交错的输出里也要认出自己那份的原因."""
        t = self._tail()
        t.feed("[20:38:22] 开始下载: 别的视频 [999]\n")
        t.feed("[2026-10-02 20:38:25.057] - 解析此分P失败\n")
        t.feed("[20:38:23] (122/337) 完成 ✓ 别的视频\n")
        self.assertEqual(t.reason()[0], failures.PARSE)

    def test_signals_are_deduped_and_ordered(self):
        t = self._tail()
        t.feed("服务器可能并不支持多线程下载\n")
        t.feed("服务器可能并不支持多线程下载\n")     # 重复
        t.feed("下载出现异常, 3秒后将进行自动重试...\n")
        kinds = [k for k, _n in t.signals()]
        self.assertEqual(kinds.count(failures.MULTITHREAD), 1, "同一类只留一条")
        self.assertIn(failures.NETWORK, kinds)
        self.assertLess(kinds.index(failures.MULTITHREAD),
                        kinds.index(failures.NETWORK), "具体的排在前面")

    def test_rest_cause_wins_even_if_it_came_first(self):
        """风控/限速优先 —— 这是和守护的约定, 不能被别的信号盖掉."""
        t = self._tail()
        t.feed("返回码=-799\n")
        t.feed("服务器可能并不支持多线程下载\n")
        t.feed("下载出现异常\n")
        self.assertEqual(t.reason()[0], failures.RATE_LIMIT)

    def test_pending_buffer_does_not_grow_without_bound(self):
        """收不住的噪音不能把内存吃光."""
        t = self._tail()
        for i in range(2000):
            t.feed("这一行没有任何失败信号 %d\n" % i)
        self.assertLessEqual(len(t._pending), 4000 + 200,
                             "没收住的部分要有上限")

    def test_normal_completion_lines_are_never_a_reason(self):
        """正常结束语绝不能被当成失败原因."""
        t = self._tail()
        for line in ("任务完成", "下载P1完毕", "开始合并音视频...",
                     "视频标题: 某某～", "加载本地cookie...",
                     "跳过下载AI字幕", "共计 1 个分P, 已选择：ALL"):
            self.assertEqual(t.reason(), (failures.UNKNOWN, ""), line)
            t.feed(line + "\n")


class TestScanAllAndPick(unittest.TestCase):
    """一次输出里可能同时有好几种毛病, 别只留一个."""

    def test_scan_all_returns_every_distinct_kind(self):
        text = ("服务器可能并不支持多线程下载\n"
                "下载出现异常, 3秒后将进行自动重试...\n"
                "解析此分P失败\n")
        kinds = [k for k, _n in failures.scan_all(text)]
        for want in (failures.MULTITHREAD, failures.NETWORK, failures.PARSE):
            self.assertIn(want, kinds)

    def test_pick_prefers_rest_causes(self):
        text = "服务器可能并不支持多线程下载\n返回码=-799\n"
        self.assertEqual(failures.pick(failures.scan_all(text))[0],
                         failures.RATE_LIMIT)

    def test_pick_without_rest_cause_takes_the_most_specific(self):
        text = "解析此分P失败\n下载出现异常\n"
        self.assertEqual(failures.pick(failures.scan_all(text))[0],
                         failures.PARSE)

    def test_empty_input(self):
        self.assertEqual(failures.scan_all(""), [])
        self.assertEqual(failures.pick([]), (failures.UNKNOWN, ""))

    def test_timestamp_prefix_is_stripped(self):
        self.assertEqual(
            failures.strip_noise("[2026-10-02 20:38:25.057] - 解析此分P失败"),
            "解析此分P失败")


class TestParsingIsSplitIntoKinds(unittest.TestCase):
    """以前"解析"是一锅烩, 现在要分得出是哪一种(处理办法完全不同)."""

    def test_parse_sub_cases_are_distinguishable(self):
        cases = [
            ("解析此分P失败(建议--debug查看详细信息)", "分P"),
            ("请尝试升级到最新版本后重试!", "建议升级"),
            ("该视频仅限大会员观看", "会员"),
            # 注意说法: 不是"视频失效"(那是替接口下结论, 见措辞纪律那条测试),
            # 而是"接口说给不出这一路"
            ("啥都木有", "给不出"),
        ]
        notes = []
        for text, want in cases:
            kind, note = failures.classify(text)
            self.assertEqual(kind, failures.PARSE, text)
            self.assertIn(want, note, "%r 应该能看出是%r" % (text, want))
            notes.append(note)
        self.assertEqual(len(set(notes)), len(notes),
                         "四种情况的说明必须各不相同, 否则等于没细分")

    def test_no_output_is_its_own_kind(self):
        """BBDown 说完成了却没有成片 —— 单独一类, 不是"解析"也不是"网络"."""
        self.assertEqual(failures.NOFILE, "无成片")
        self.assertFalse(failures.should_rest(failures.NOFILE))
        self.assertTrue(failures.advice(failures.NOFILE))

    def test_every_kind_has_advice_or_is_unknown(self):
        for kind in (failures.RATE_LIMIT, failures.THROTTLED,
                     failures.MULTITHREAD, failures.NETWORK, failures.PARSE,
                     failures.NOFILE, failures.CLIP):
            self.assertTrue(failures.advice(kind),
                            "%s 应该有处理建议" % kind)


class TestWordingDoesNotJumpToConclusions(unittest.TestCase):
    """措辞纪律: 只说接口告诉我们什么, 不替它下结论.

    用户指出的一处错: 我把播放接口的 -404 写成"视频不存在"。但
        · 名单里能出现的视频, 解析时就说明它存在过;
        · 同一个视频的 view 接口返回 code=0(标题/分P/时长都正常),
          只是播放接口给不出流。
    说"不存在"会让人以为名单脏了、跑去清名单 —— 方向全错。
    实测那次失败里抽查 25 个, 22 个当时就能正常下, 更说明这个结论站不住。
    """

    CONCLUDING_WORDS = ("不存在", "没了", "查无此人", "已失效")

    def test_no_conclusion_words_in_any_note(self):
        from bbdown_kit import failures
        offenders = []
        for key, kind, order, pat, note in failures._SIGNALS:
            if not note:
                continue
            for word in self.CONCLUDING_WORDS:
                if word in note:
                    offenders.append("%s 的说明里有 %r" % (key, word))
        for code, note in failures._PAYLOAD_CODES.items():
            for word in self.CONCLUDING_WORDS:
                if word in note:
                    offenders.append("code=%s 的说明里有 %r" % (code, word))
        self.assertEqual(offenders, [], "说明里不许替接口下结论: %s" % offenders)

    def test_payload_404_says_what_the_interface_said(self):
        from bbdown_kit import failures
        kind, note = failures.classify('{"code":-404,"message":"啥都木有"}')
        self.assertEqual(kind, failures.PARSE)
        self.assertIn("-404", note, "要把接口给的码说清楚")
        self.assertIn("给不出", note, "说法应该是'播放接口给不出流'这类")

    def test_advice_tells_people_to_retry_first(self):
        """建议必须说"先重试" —— 实测大部分重试就好, 别一上来就劝人放弃."""
        from bbdown_kit import failures
        advice = failures.advice(failures.PARSE)
        self.assertIn("重试", advice)
        self.assertNotIn("跳过", advice,
                         "不该一上来就建议跳过(那会让视频被永久放进跳过名单)")


class TestPayloadCodeWins(TempRootTest):
    """BBDown 打出的那个 JSON 的 code 才是权威原因, 压过一切文案.

    真实事故: 端到端跑一个 -404 的视频, BBDown 输出
        解析此分P失败(建议--debug查看详细信息)
        {"code":-404,"message":"啥都木有","ttl":1}
    用户该看到的是"视频不存在", 而不是笼统的"解析分P失败" ——
    前者告诉你别浪费时间重试, 后者会让人以为是网络问题。
    """

    def test_payload_404_beats_the_generic_parse_message(self):
        text = ("[2026-10-03 02:27:30.162] - 解析此分P失败(建议--debug查看详细信息)\n"
                '[2026-10-03 02:27:30.162] - {"code":-404,"message":"啥都木有","ttl":1}\n'
                "[2026-10-03 02:27:30.162] - 任务完成\n")
        kind, note = failures.classify(text)
        self.assertEqual(kind, failures.PARSE)
        self.assertIn("-404", note, "接口给的码才是权威原因")
        self.assertNotIn("解析分P失败", note)

    def test_payload_codes_are_translated(self):
        cases = [
            (-404, "-404"),
            (-10403, "充电"),
            (-799, "限速"),
            (62002, "稿件"),
        ]
        for code, want in cases:
            _kind, note = failures.classify('{"code":%d,"message":"x"}' % code)
            self.assertIn(want, note, "code=%d 的说明里该提到 %r" % (code, want))

    def test_payload_zero_is_not_a_failure_signal(self):
        """{"code":0} 是正常返回, 别把它当失败原因."""
        self.assertEqual(failures.classify('{"code":0,"message":"OK"}'),
                         (failures.UNKNOWN, ""))

    def test_payload_rest_codes_ask_for_a_rest(self):
        for code in (-799, -352, -412, -509, 87008):
            kind, _note = failures.classify('{"code":%d,"message":"x"}' % code)
            self.assertTrue(failures.should_rest(kind),
                            "code=%d 是账号级拦截, 得让守护休息" % code)

    def test_barcode_form_still_works(self):
        """BBDown 有时直接打 "返回码=-799" 这种文字, 这条路不能丢.

        (我第一版把这条正则换成 JSON 专用时弄丢过它 —— 于是风控识别退化,
        所以专门钉一条。)
        """
        kind, note = failures.classify("现在同一个接口返回码=-799")
        self.assertEqual(kind, failures.RATE_LIMIT)
        self.assertIn("限速", note)


class TestPriorityOrdering(unittest.TestCase):
    """优先级必须由识别这一步定下来, 不能事后从静态表里猜."""

    def test_order_signals_sorts_by_priority(self):
        """具体的原因要排在笼统的前面, 跟发现顺序无关.

        用**能在静态表里查到**的两条来验排序本身(两条同属"解析"类,
        但"多线程"这条更具体, order 2 < 3)。
        """
        vague = (failures.PARSE, "解析分P失败(BBDown 拿不到这一路的播放地址)")
        exact = (failures.MULTITHREAD, "服务器不吃多线程")
        self.assertEqual(failures.order_signals([vague, exact])[0], exact,
                         "具体的必须排前面")
        self.assertEqual(failures.order_signals([exact, vague])[0], exact,
                         "反着喂也要一样")

    def test_payload_notes_are_ranked_by_scan_not_by_lookup(self):
        """接口码的说明是**动态生成**的, 静态表里查不到 —— 必须靠显式优先级.

        这就是 "播放接口给不出流(-404 啥都木有)" 这类说明的处境: 它是按 -404
        现翻的, 拿它去静态表里查只能查到 99(排最后)。所以真正的排序依据是
        scan_ranked 带出来的那个 0, 不是说明文字。
        """
        payload = failures.scan_ranked('{"code":-404,"message":"啥都木有"}')
        self.assertEqual(payload[0][0], 0)
        # 拿它的说明去查表, 查不到(这正是为什么不能靠查表)
        self.assertEqual(
            failures._rank(failures.PARSE, payload[0][2]), 99)
        # 但显式带上优先级就能压过静态表里的那些
        generic = (failures.PARSE,
                   "解析分P失败(BBDown 拿不到这一路的播放地址)")
        got = failures.order_signals([generic] + payload)
        self.assertEqual(got[0], payload[0][1:],
                         "接口明说的原因必须排在最前")

    def test_ranked_triples_beat_the_table_lookup(self):
        """带优先级的信号压过按静态表查出来的那些."""
        from_payload = failures.scan_ranked('{"code":-404,"message":"啥都木有"}')
        generic = (failures.PARSE,
                   "解析分P失败(BBDown 拿不到这一路的播放地址)")
        got = failures.order_signals([generic] + from_payload)
        self.assertEqual(got[0], from_payload[0][1:],
                         "接口明说的原因必须排在最前")

    def test_order_signals_accepts_ranked_triples(self):
        ranked = (0, failures.PARSE, "接口说的")
        other = (failures.PARSE, "解析分P失败(BBDown 拿不到这一路的播放地址)")
        got = failures.order_signals([other, ranked])
        self.assertEqual(got[0], (failures.PARSE, "接口说的"))

    def test_order_signals_dedupes(self):
        item = (failures.PARSE, "同名")
        self.assertEqual(len(failures.order_signals([item, item, item])), 1)

    def test_scan_ranked_exposes_the_priority(self):
        got = failures.scan_ranked('{"code":-404,"message":"x"}')
        self.assertTrue(got)
        self.assertEqual(got[0][0], 0, "接口返回的码优先级是 0")
        self.assertEqual(got[0][1], failures.PARSE)

    def test_note_without_a_known_rank_goes_last(self):
        """查不到优先级的说明排最后 —— 宁可排后, 也别乱猜一个位置."""
        strange = (failures.PARSE, "一个静态表里没有的说明")
        known = (failures.MULTITHREAD, "服务器不吃多线程")
        self.assertEqual(failures.order_signals([strange, known])[0], known)
        self.assertEqual(failures._rank(failures.PARSE, "莫名其妙"), 99)

    def test_network_never_beats_something_concrete(self):
        """笼统的"网络抖动"不能盖住"多线程"这种具体原因."""
        text = ("服务器可能并不支持多线程下载\n"
                "下载出现异常, 3秒后将进行自动重试...\n")
        self.assertEqual(failures.classify(text)[0], failures.MULTITHREAD)


class TestReasonIsRecordedEvenWhenRcIsZero(TempRootTest):
    """（真实事故）rc==0 但没成片时, 原因也必须有.

    那一轮 1440 个失败里有 1437 个 rc==0, 而原因字段全是空的 ——
    因为旧代码写着"只在 rc != 0 时才去认原因"。事后只能靠翻日志还原。
    """

    def _run_one(self, rc, text="", make_file=False):
        from unittest import mock
        from bbdown_kit import download
        up = folder("UP主下载", "123_测试UP")
        os.makedirs(up, exist_ok=True)
        video = {"aid": "456", "bvid": "", "title": "测试视频"}

        def fake_run(url, folder_, extra_args, timeout=None, aid=None,
                     sink=None):
            if sink is not None:
                sink.append(_FakeTail(text))
            if make_file:
                with open(os.path.join(folder_, "测试视频_456.mp4"), "wb") as f:
                    f.write(b"x")
            return rc

        with mock.patch.object(download, "run_bbdown", fake_run):
            return download.download_one(up, video, [], set())

    def test_rc_zero_without_a_file_still_records_a_reason(self):
        video, ok, rc, _nf, _t = self._run_one(0, text="任务完成\n")
        self.assertFalse(ok, "rc=0 但没有成片, 仍然是失败")
        self.assertEqual(rc, 0)
        self.assertEqual(video.get("_失败原因"), failures.NOFILE,
                         "rc=0 的失败也要有原因, 不能是空的")
        self.assertIn("没成片", video.get("_失败说明", ""))

    def test_parse_failure_with_rc_zero_is_recorded(self):
        result = self._run_one(0, text="解析此分P失败(建议--debug查看详细信息)\n")
        video, ok, rc, _nf, _t = result
        self.assertFalse(ok)
        self.assertEqual(rc, 0)
        self.assertEqual(video.get("_失败原因"), failures.PARSE)
        self.assertIn("分P", video.get("_失败说明", ""))

    def test_success_records_no_reason(self):
        result = self._run_one(0, text="任务完成\n", make_file=True)
        video, ok, _rc, _nf, _t = result
        self.assertTrue(ok)
        self.assertEqual(video.get("_失败原因"), "", "成功了不该有失败原因")


class _FakeTail(object):
    """冒充 _Tail: 只要 reason() 够用就行."""

    def __init__(self, text):
        self._text = text

    def reason(self):
        return failures.scan(self._text)


if __name__ == "__main__":
    unittest.main()
