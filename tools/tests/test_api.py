# -*- coding: utf-8 -*-
"""B站接口层的测试(全部离线: 用假会话喂预置响应)."""

import os
import unittest
from unittest import mock

from support import (FakeResponse, FakeSession, TempRootTest, folder,
                     season_page, write_state)

from bbdown_kit import bilitools


class TestCollectionLinks(unittest.TestCase):
    """合集/系列链接的识别(纯字符串, 不联网)."""

    def test_new_season_link(self):
        spec = bilitools.parse_collection_link(
            "https://space.bilibili.com/123456781/lists/1000001?type=season")
        self.assertEqual(spec, {"类型": "合集", "mid": "123456781",
                                "id": "1000001"})

    def test_new_series_link(self):
        spec = bilitools.parse_collection_link(
            "https://space.bilibili.com/123456782/lists/1000002?type=series")
        self.assertEqual(spec["类型"], "系列")
        self.assertEqual(spec["id"], "1000002")

    def test_link_without_type_counts_as_season(self):
        spec = bilitools.parse_collection_link(
            "https://space.bilibili.com/123456781/lists/1000001")
        self.assertEqual(spec["类型"], "合集")

    def test_old_links(self):
        season = bilitools.parse_collection_link(
            "https://space.bilibili.com/123/channel/collectiondetail?sid=456")
        series = bilitools.parse_collection_link(
            "https://space.bilibili.com/123/channel/seriesdetail?sid=789")
        self.assertEqual((season["类型"], season["id"]), ("合集", "456"))
        self.assertEqual((series["类型"], series["id"]), ("系列", "789"))

    def test_up_home_and_video_are_not_collections(self):
        self.assertIsNone(bilitools.parse_collection_link(
            "https://space.bilibili.com/123456781"))
        self.assertIsNone(bilitools.parse_collection_link(
            "https://www.bilibili.com/video/BV1DGDpBgEos"))
        self.assertIsNone(bilitools.parse_collection_link(""))
        self.assertIsNone(bilitools.parse_collection_link(None))


class TestLinkParsing(unittest.TestCase):
    def test_extract_mid(self):
        self.assertEqual(bilitools.extract_mid(
            "https://space.bilibili.com/123456780"), "123456780")
        self.assertEqual(bilitools.extract_mid("123456780"), "123456780")
        self.assertIsNone(bilitools.extract_mid("不是链接"))

    def test_extract_video_id(self):
        self.assertEqual(bilitools.extract_video_id(
            "https://www.bilibili.com/video/BV1DGDpBgEos"), "BV1DGDpBgEos")
        self.assertEqual(bilitools.extract_video_id("av12345"), "12345")
        self.assertIsNone(bilitools.extract_video_id("随便什么"))

    def test_classify_link(self):
        self.assertEqual(bilitools.classify_link(
            "https://space.bilibili.com/123"), "up")
        self.assertEqual(bilitools.classify_link(
            "https://www.bilibili.com/video/BV1DGDpBgEos"), "video")
        self.assertEqual(bilitools.classify_link("https://b23.tv/abcd"), "video")
        self.assertIsNone(bilitools.classify_link("https://example.com"))

    def test_collection_folder_name_is_safe(self):
        season = bilitools.collection_folder(
            {"类型": "合集", "id": "1000001", "名称": "某某合集"})
        series = bilitools.collection_folder(
            {"类型": "系列", "id": "1000002", "名称": "某某/夏日"})
        self.assertEqual(os.path.basename(season), "某某合集_1000001")
        self.assertEqual(os.path.basename(series), "某某_夏日_系列_1000002")


class TestArchiveParsing(unittest.TestCase):
    def test_parse_archive_items_cleans_up(self):
        items = bilitools.parse_archive_items([
            {"aid": 100000000000006, "bvid": "BV1", "title": " 直播回放 "},
            {"bvid": "BV2", "title": "没有aid"},          # 丢弃
            {"aid": 42, "title": ""},                      # 没标题 -> 占位名
        ])
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0], {"aid": "100000000000006", "bvid": "BV1",
                                    "title": "直播回放"})
        self.assertEqual(items[1]["title"], "av42")

    def test_fetch_season_paginates_and_retries(self):
        page1 = [("%d" % i, "标题%d" % i) for i in range(1, 31)]
        page2 = [("%d" % i, "标题%d" % i) for i in range(31, 34)]
        session = FakeSession([
            ("seasons_archives_list", [
                {"code": -504, "message": "服务调用超时"},     # 第一次先失败
                season_page(page1, 33),
                season_page(page2, 33),
            ]),
        ])
        with mock.patch.object(bilitools.time, "sleep", lambda s: None):
            name, videos = bilitools.fetch_season(session, "123456781", "1000001")
        self.assertEqual(name, "某某合集")
        self.assertEqual(len(videos), 33)
        self.assertEqual(videos[-1]["aid"], "33")
        pages = [p["page_num"] for _u, p in session.calls]
        self.assertEqual(pages, [1, 1, 2])     # 失败重试那一页没有跳页

    def test_fetch_season_stops_on_early_empty_page(self):
        session = FakeSession([
            ("seasons_archives_list", season_page([("1", "只有一个")], 5)),
        ])
        name, videos = bilitools.fetch_season(session, "1", "2")
        self.assertEqual(len(videos), 1)
        self.assertEqual(len(session.calls), 1)

    def test_fetch_series_reads_name_separately(self):
        session = FakeSession([
            ("x/series/archives", {
                "code": 0,
                "data": {"archives": [{"aid": 7, "bvid": "BV7", "title": "某某"}],
                         "page": {"size": 30, "total": 1}},
            }),
            ("x/series/series", {"code": 0, "data": {"meta": {"name": "某某"}}}),
        ])
        with mock.patch.object(bilitools.time, "sleep", lambda s: None):
            name, videos = bilitools.fetch_series(session, "123456782", "1000002")
        self.assertEqual(name, "某某")
        self.assertEqual(videos[0]["aid"], "7")


class TestVideoListing(unittest.TestCase):
    """投稿列表的拉取上限与分页."""

    def test_fetch_videos_goes_past_the_old_2500_cap(self):
        """投稿上万的 UP主 不能被翻页上限截断(实测有个号 4017 条)."""
        total = 4017

        def fake_api(session, url, params, key, retries=3):
            pn = int(params["pn"])
            start = (pn - 1) * bilitools.PAGE_SIZE
            n = max(0, min(bilitools.PAGE_SIZE, total - start))
            return {"code": 0, "data": {
                "list": {"vlist": [
                    {"aid": start + i + 1, "bvid": "BV%d" % (start + i + 1),
                     "title": "视频%d" % (start + i + 1)} for i in range(n)]},
                "page": {"count": total},
            }}

        with mock.patch.object(bilitools, "api_get", fake_api), \
                mock.patch.object(bilitools.time, "sleep", lambda s: None):
            videos, count, complete = bilitools.fetch_videos(None, None, "123456789")
        self.assertEqual(count, total)
        self.assertEqual(len(videos), total)
        self.assertTrue(complete)
        self.assertGreaterEqual(bilitools.MAX_PAGES * bilitools.PAGE_SIZE, total)

    def test_empty_page_with_count_is_incomplete(self):
        """接口说有几条却返回空页: 必须按"不完整"处理, 否则会漏视频."""
        def fake_api(session, url, params, key, retries=3):
            return {"code": 0, "data": {"list": {"vlist": []},
                                        "page": {"count": 900}}}

        with mock.patch.object(bilitools, "api_get", fake_api), \
                mock.patch.object(bilitools.time, "sleep", lambda s: None):
            videos, count, complete = bilitools.fetch_videos(None, None, "1")
        self.assertEqual(videos, [])
        self.assertEqual(count, 900)
        self.assertFalse(complete)

    def test_page_matches_prefix_ignores_order(self):
        stored = [{"aid": "3"}, {"aid": "2"}, {"aid": "1"}]
        self.assertTrue(bilitools.page_matches_prefix(["3", "2"], 0, stored))
        self.assertFalse(bilitools.page_matches_prefix(["9"], 0, stored))
        self.assertTrue(bilitools.page_matches_prefix(["9"], 1, stored))

    def test_merge_head_dedupes_and_keeps_order(self):
        head = [{"aid": "9"}, {"aid": "8"}]
        stored = [{"aid": "8"}, {"aid": "7"}]
        merged = bilitools.merge_head(head, stored)
        self.assertEqual([v["aid"] for v in merged], ["9", "8", "7"])


class TestIncrementalSync(TempRootTest):
    """增量同步: 能少发请求就少发, 但绝不能漏视频."""

    def make_state(self, videos=None, meta=None):
        up = folder("UP主下载", "123_测试UP")
        videos = videos or [
            {"aid": "3", "bvid": "BV3", "title": "第三个"},
            {"aid": "2", "bvid": "BV2", "title": "第二个"},
            {"aid": "1", "bvid": "BV1", "title": "第一个"},
        ]
        meta = meta if meta is not None else {
            "最新aid": videos[0]["aid"], "视频总数": len(videos),
            "列表完整": True, "全量时间": "2026-09-26 17:53:31",
            "同步方式": "全量",
        }
        return write_state(up, videos, meta=meta)

    def _sync(self, st, top_aid, count, items, **kw):
        with mock.patch.object(bilitools, "probe_latest",
                               lambda s, k, m: (top_aid, count, items)), \
                mock.patch.object(bilitools.time, "sleep", lambda s: None):
            return bilitools.sync_videos(None, None, "123", st, **kw)

    def test_no_change_uses_one_request(self):
        st = self.make_state()
        items = [{"aid": "3", "bvid": "BV3", "title": "第三个"},
                 {"aid": "2", "bvid": "BV2", "title": "第二个"},
                 {"aid": "1", "bvid": "BV1", "title": "第一个"}]
        with mock.patch.object(bilitools, "fetch_videos_page",
                               side_effect=AssertionError("不该再翻页")):
            result = self._sync(st, "3", 3, items)
        self.assertEqual(result.mode, "none")
        self.assertEqual([v["aid"] for v in result.videos], ["3", "2", "1"])

    def test_new_video_is_merged_incrementally(self):
        st = self.make_state()
        items = [{"aid": "4", "bvid": "BV4", "title": "新的"},
                 {"aid": "3", "bvid": "BV3", "title": "第三个"},
                 {"aid": "2", "bvid": "BV2", "title": "第二个"}]
        result = self._sync(st, "4", 4, items)
        self.assertEqual(result.mode, "incremental")
        self.assertEqual([v["aid"] for v in result.videos], ["4", "3", "2", "1"])
        self.assertEqual(result.meta["最新aid"], "4")
        self.assertEqual(result.meta["视频总数"], 4)
        self.assertEqual(result.meta["同步方式"], "增量")

    def test_count_change_in_middle_forces_full(self):
        """最新一条没变但总数变了 -> 改动在列表中间, 必须全量."""
        st = self.make_state()
        full = [{"aid": "3", "bvid": "BV3", "title": "第三个"},
                {"aid": "2", "bvid": "BV2", "title": "第二个"},
                {"aid": "1", "bvid": "BV1", "title": "第一个"},
                {"aid": "0", "bvid": "BV0", "title": "补的"}]
        items = [{"aid": "3", "bvid": "BV3", "title": "第三个"},
                 {"aid": "2", "bvid": "BV2", "title": "第二个"}]
        with mock.patch.object(bilitools, "fetch_videos",
                               lambda *a, **k: (full, 4, True)):
            result = self._sync(st, "3", 4, items)
        self.assertEqual(result.mode, "full")
        self.assertEqual(len(result.videos), 4)

    def test_missing_baseline_forces_full(self):
        st = self.make_state(meta={})
        with mock.patch.object(bilitools, "fetch_videos",
                               lambda *a, **k: (st.videos, 3, True)):
            result = self._sync(st, "3", 3, [])
        self.assertEqual(result.mode, "full")

    def test_head_mismatch_forces_full(self):
        """最新一条变了、总数也变了, 但头部几十条对不上 -> 全量."""
        st = self.make_state()
        items = [{"aid": "4", "bvid": "BV4", "title": "新的"},
                 {"aid": "99", "bvid": "BV99", "title": "没见过的"}]
        with mock.patch.object(bilitools, "fetch_videos",
                               lambda *a, **k: (st.videos, 4, True)):
            result = self._sync(st, "4", 4, items)
        self.assertEqual(result.mode, "full")

    def test_force_full_reason_is_honoured(self):
        st = self.make_state()
        with mock.patch.object(bilitools, "fetch_videos",
                               lambda *a, **k: (st.videos, 3, True)):
            result = self._sync(st, "3", 3, [], force_full="指定了 --full")
        self.assertEqual(result.mode, "full")


class TestLoginClassification(unittest.TestCase):
    """登录/风控的区分: 判错了会让人白跑一趟扫码."""

    def _probe(self, payload):
        session = mock.Mock()
        session.get.return_value.json.return_value = payload
        with mock.patch.object(bilitools, "make_session", lambda: session):
            return bilitools.probe_login()

    def test_ok(self):
        self.assertEqual(self._probe({"code": 0, "data": {"isLogin": True}})[0],
                         "ok")

    def test_rate_limited_is_not_dead(self):
        for code in (-352, -412, -799, -509, -401):
            verdict, _detail = self._probe({"code": code, "message": "x"})
            self.assertEqual(verdict, "ratelimited", "code=%s 不该算成登录过期"
                             % code)

    def test_login_dead_codes(self):
        """nav 接口上的 -400 仍然算登录过期(这条路由 probe_login 判)."""
        for code in (-101, -403, -400):
            verdict, _detail = self._probe({"code": code, "message": "x"})
            self.assertEqual(verdict, "dead", "code=%s 应该算登录过期" % code)

    def test_code_zero_but_not_logged_in(self):
        self.assertEqual(
            self._probe({"code": 0, "data": {"isLogin": False}})[0], "dead")

    def test_describe_code(self):
        self.assertEqual(bilitools.describe_code(0), "正常")
        self.assertIn("风控", bilitools.describe_code(87008))
        self.assertIn("充电", bilitools.describe_code(-10403))


class TestClassifyCode(unittest.TestCase):
    """返回码 -> 人话.

    这一条是被一次真实误报逼出来的: 加新UP主时接口返回 -799(请求过于频繁),
    程序一律说"未找到该UP主, 请确认链接是否正确" —— 用户以为链接错了、
    甚至以为号没了, 其实那个号好好的(实测 name 正常返回)。
    """

    def test_ok(self):
        self.assertEqual(bilitools.classify_code(0)[0], "ok")

    def test_rate_limited_is_not_mistaken_for_missing(self):
        for code in (-799, -352, -412, -509, -401):
            kind, why = bilitools.classify_code(code, "请求过于频繁")
            self.assertEqual(kind, "rate_limited", "code=%s 该算限流" % code)
            self.assertIn("限流", why)
            self.assertNotIn("不存在", why)

    def test_login_dead(self):
        for code in (-101, -403):
            kind, why = bilitools.classify_code(code)
            self.assertEqual(kind, "login_dead")
            self.assertIn("登录", why)

    def test_minus_400_is_not_login_dead_here(self):
        """-400 在这个上下文里是"参数错", 不是登录失效.

        nav 接口上的 -400 才是 cookie 坏了(那条路由 login_verdict 判, 见
        test_login_dead_codes 之外的那个用例)。查 UP主/视频信息时传错参数
        也会返 -400 —— 混在一起就会让人白跑一趟去扫码。
        """
        kind, why = bilitools.classify_code(-400, "请求错误")
        self.assertEqual(kind, "unknown")
        self.assertNotIn("登录", why)

    def test_not_found_codes(self):
        for code in (-404, 62002, 62004):
            kind, why = bilitools.classify_code(code, "啥都木有")
            self.assertEqual(kind, "not_found")
            self.assertIn("不存在", why)

    def test_network_failure(self):
        kind, why = bilitools.classify_code(None, "连接超时")
        self.assertEqual(kind, "unknown")
        self.assertIn("网络", why)

    def test_unknown_code_keeps_the_number(self):
        kind, why = bilitools.classify_code(12345, "怪东西")
        self.assertEqual(kind, "unknown")
        self.assertIn("12345", why)
        self.assertIn("怪东西", why)

    def test_rate_limit_codes_are_the_retry_set(self):
        """会重试的那一撮必须和"限流"判断保持一致, 否则重试白做."""
        self.assertEqual(set(bilitools.RATE_LIMIT_CODES),
                         set(bilitools.CODE_RATE_LIMITED))


class TestApiGetRetry(unittest.TestCase):
    """api_get 遇到限流要退避重试, 而且最后要把最后一次的返回交出来."""

    def setUp(self):
        from unittest import mock
        self.mock = mock

    def _session(self, responses):
        session = self.mock.Mock()
        session.get.side_effect = [FakeResponse(r) for r in responses]
        return session

    def test_retries_then_succeeds(self):
        session = self._session([
            {"code": -799, "message": "请求过于频繁"},
            {"code": -799, "message": "请求过于频繁"},
            {"code": 0, "data": {"name": "羊肉串103"}},
        ])
        with self.mock.patch.object(bilitools.time, "sleep", lambda s: None):
            data = bilitools.api_get(session, "http://x", {"mid": "1"}, "key")
        self.assertEqual(data.get("code"), 0)
        self.assertEqual(session.get.call_count, 3)

    def test_gives_up_but_returns_the_last_response(self):
        """重试完还失败: 返回最后一次的返回码(而不是抛异常) —— 让上层能说实话."""
        session = self._session([{"code": -799, "message": "请求过于频繁"}] * 5)
        with self.mock.patch.object(bilitools.time, "sleep", lambda s: None):
            data = bilitools.api_get(session, "http://x", {}, "key")
        self.assertEqual(data.get("code"), -799)

    def test_business_error_is_not_retried(self):
        """-404 这种"确实没有"不该浪费重试次数."""
        session = self._session([{"code": -404, "message": "啥都木有"}])
        with self.mock.patch.object(bilitools.time, "sleep", lambda s: None):
            data = bilitools.api_get(session, "http://x", {}, "key")
        self.assertEqual(data.get("code"), -404)
        self.assertEqual(session.get.call_count, 1)


class TestGetUpInfoContract(unittest.TestCase):
    """get_up_info 必须把"名字"和"为什么没拿到"分开返回."""

    def _call(self, payload):
        session = self.mock.Mock()
        session.get.return_value = FakeResponse(payload)
        with self.mock.patch.object(bilitools, "time") as fake_time:
            fake_time.sleep = lambda s: None
            return bilitools.get_up_info(session, "key", "1")

    def setUp(self):
        from unittest import mock
        self.mock = mock

    def test_success_returns_name_and_empty_reason(self):
        name, why = self._call({"code": 0, "data": {"name": "羊肉串103"}})
        self.assertEqual(name, "羊肉串103")
        self.assertEqual(why, "")

    def test_rate_limited_says_so_instead_of_not_found(self):
        name, why = self._call({"code": -799, "message": "请求过于频繁"})
        self.assertIsNone(name)
        self.assertIn("限流", why)
        self.assertNotIn("不存在", why)

    def test_real_missing_account_says_not_found(self):
        name, why = self._call({"code": -404, "message": "啥都木有"})
        self.assertIsNone(name)
        self.assertIn("不存在", why)

    def test_login_dead_is_reported(self):
        name, why = self._call({"code": -101, "message": "账号未登录"})
        self.assertIsNone(name)
        self.assertIn("登录", why)

    def test_success_but_no_name(self):
        name, why = self._call({"code": 0, "data": {}})
        self.assertIsNone(name)
        self.assertIn("名字", why)


class TestLoginVerdict(unittest.TestCase):
    """登录检查必须说清"没通过"是哪种情况.

    这一条也是被真实误报逼出来的: 撞上 -799 限流时, 程序说"当前未登录或登录
    已失效, 请扫码登录" —— 而那个账号的 SESSDATA 还有 179 天有效期, 用户白跑
    一趟去扫码。
    """

    def setUp(self):
        from unittest import mock
        self.mock = mock

    def _verdict(self, payload):
        session = self.mock.Mock()
        session.get.return_value = FakeResponse(payload)
        return bilitools.login_verdict(session)

    def test_logged_in(self):
        ok, why = self._verdict({"code": 0,
                                 "data": {"isLogin": True, "uname": "x"}})
        self.assertTrue(ok)
        self.assertEqual(why, "")

    def test_rate_limit_is_not_reported_as_expired(self):
        for code in (-799, -352, -412, -509):
            ok, why = self._verdict({"code": code, "message": "请求过于频繁"})
            self.assertFalse(ok)
            self.assertIn("限流", why, "code=%s 该说限流" % code)
            self.assertNotIn("失效", why, "code=%s 不能说登录失效" % code)

    def test_really_expired(self):
        ok, why = self._verdict({"code": 0, "data": {"isLogin": False}})
        self.assertFalse(ok)
        self.assertIn("失效", why)

    def test_login_dead_codes(self):
        ok, why = self._verdict({"code": -101, "message": "账号未登录"})
        self.assertFalse(ok)
        self.assertIn("登录", why)

    def test_network_failure_is_reported_as_such(self):
        session = self.mock.Mock()
        session.get.side_effect = RuntimeError("连接超时")
        ok, why = bilitools.login_verdict(session)
        self.assertFalse(ok)
        self.assertIn("网络", why)

    def test_check_login_still_returns_a_bool(self):
        """薄包装必须保持旧契约(别的地方和测试还在用它)."""
        session = self.mock.Mock()
        session.get.return_value = FakeResponse(
            {"code": 0, "data": {"isLogin": True}})
        self.assertIs(bilitools.check_login(session), True)
        session.get.return_value = FakeResponse({"code": -799})
        self.assertIs(bilitools.check_login(session), False)

    def test_non_object_body_is_not_a_crash(self):
        for payload in (["not", "an", "object"], "a string", 7, None):
            ok, why = self._verdict(payload)
            self.assertFalse(ok)
            self.assertTrue(why)


class TestLoginRetryInManager(unittest.TestCase):
    """管理器撞到限流要先自己退避重试, 而不是立刻叫人去扫码."""

    def test_manager_retries_on_rate_limit(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "BBDown-manager.py"),
            encoding="utf-8").read()
        self.assertIn("login_verdict", src)
        self.assertIn("for attempt in range(3)", src,
                      "登录检查要重试")
        self.assertIn("限流", src, "限流要单独给提示, 不能叫用户去扫码")

    def test_manager_no_longer_blames_the_link(self):
        """UP主那条路上不该再出现"未找到该UP主, 请确认链接是否正确".

        只看**可执行代码里的字符串**(用 AST), 不看文档字符串 ——
        文件头的功能说明里提到那句话是在讲历史, 不是真的会打印它。
        也别误伤 "未找到该视频": 那是单视频下载分支的提示, 是对的。
        """
        import ast

        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "BBDown-manager.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    docstrings.add(doc)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if node.value in docstrings:
                continue
            self.assertNotIn(
                "未找到该UP主", node.value,
                "第 %s 行还有那句会误导人的提示" % getattr(node, "lineno", "?"))

    def test_bad_request_code_is_not_blamed_on_login(self):
        """-400("请求错误") 是**参数错**, 不是登录失效.

        真实事故: 菜单里粘了一整条主页链接, 而 ask_up_link 只把 extract_mid
        用在**校验**上、返回的却是原文 —— 于是整条 URL 被当 UID 传到 acc/info,
        接口返 -400, 界面报"登录已失效, 请重新扫码"。用户白跑一趟去扫码,
        而登录好得很(SESSDATA 还有半年)。
        """
        kind, why = bilitools.classify_code(-400, "请求错误")
        self.assertEqual(kind, "unknown", "-400 不该被算成 login_dead")
        self.assertNotIn("登录", why)
        self.assertIn("-400", why)
        self.assertIn("参数", why)

    def test_minus_400_means_different_things_per_endpoint(self):
        """同一个 -400: 登录检查上算登录没了, 查信息上算参数错."""
        self.assertIn("登录", bilitools.code_meaning(-400, "登录检查"))
        self.assertIn("参数", bilitools.code_meaning(-400, "UP主信息"))

    def test_real_login_dead_codes_still_work(self):
        """-101/-403 仍然是登录失效(别把这条修坏了)."""
        for code in (-101, -403):
            kind, why = bilitools.classify_code(code, "账号未登录")
            self.assertEqual(kind, "login_dead", code)
            self.assertIn("登录", why)


class TestUpLinkIsConvertedToUid(unittest.TestCase):
    """（真实事故）菜单里粘链接, 必须变成纯数字 UID 再往下传."""

    def _ask_with(self, typed):
        manager = _load_manager()
        with mock.patch("builtins.input", lambda *a: typed):
            return manager.ask_up_link()

    def test_full_url_becomes_a_uid(self):
        got = self._ask_with("https://space.bilibili.com/87654321")
        self.assertEqual(got, "87654321",
                         "返回原文的话, 整条 URL 就会被当 UID 传下去")

    def test_url_with_trailing_slash_and_query(self):
        got = self._ask_with("https://space.bilibili.com/87654321/?spm=x")
        self.assertEqual(got, "87654321")

    def test_bare_number_still_works(self):
        self.assertEqual(self._ask_with("87654321"), "87654321")

    def test_extract_mid_handles_what_people_actually_paste(self):
        """各种粘法都要认出来."""
        for text in ("https://space.bilibili.com/87654321",
                     "http://space.bilibili.com/87654321/",
                     "space.bilibili.com/87654321",
                     "  https://space.bilibili.com/87654321  ",
                     "87654321"):
            self.assertEqual(bilitools.extract_mid(text), "87654321",
                             "%r 应该能取出 UID" % text)


def _load_manager():
    """把 BBDown-manager.py 当模块加载(它是薄入口, 测它的纯函数)."""
    import importlib.util
    from support import REAL_TOOLS
    spec = importlib.util.spec_from_file_location(
        "mgr_for_test", os.path.join(REAL_TOOLS, "BBDown-manager.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    unittest.main()
