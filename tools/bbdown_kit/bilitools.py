# -*- coding: utf-8 -*-
"""B 站接口: 登录、投稿列表(含增量同步)、合集/系列、播放地址探测.

以前这些逻辑在两个程序里各有一份几乎相同的实现(建会话、查 nav、算 wbi 签名、
猜"是不是被风控"), 改动要同步两处, 漏一处就会出现"守护认为被限流、管理器
认为登录失效"这种自相矛盾的判断。现在集中在这里。

关于限流的几个返回码(实测):
    0      正常
    87008  BBDown 用的 wbi playurl 被风控拦截 —— 限流最直接的表现
    -352 / -412 / -799 / -509 / -401   请求过于频繁/风控, 不是真的没登录
    -101 / -400 / -403                 登录确实失效了, 必须重新扫码
    -10403 该视频是充电/会员专属(永远下不了, 不该算限流)
"""

import datetime
import hashlib
import os
import re
import time
import urllib.parse

from . import paths
from .logging import log
from .util import clean_text, to_int

try:
    import requests
except ImportError:                      # 让 import 本模块本身不至于炸
    requests = None

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com/"}

API_NAV = "https://api.bilibili.com/x/web-interface/nav"
API_VIEW = "https://api.bilibili.com/x/web-interface/view"
API_SPACE_ARC = "https://api.bilibili.com/x/space/wbi/arc/search"
API_SEASON_ARCHIVES = "https://api.bilibili.com/x/polymer/web-space/seasons_archives_list"
API_SERIES_ARCHIVES = "https://api.bilibili.com/x/series/archives"
API_SERIES_META = "https://api.bilibili.com/x/series/series"
API_PLAYURL = "https://api.bilibili.com/x/player/wbi/playurl"

# 返回码分类
CODE_RATE_LIMITED = (-352, -412, -799, -509, -401)
CODE_CHARGING_EXCLUSIVE = -10403

# 值得重试的返回码 = "请求太频繁"这一家(见 api_get)
RATE_LIMIT_CODES = frozenset(CODE_RATE_LIMITED)
# "这个人/这个视频真的不存在" —— 只有这几种才该告诉用户"查无此人"
CODE_NOT_FOUND = (-404, 62002, 62004)

# "登录没了"的返回码
CODE_LOGIN_DEAD = (-101, -403)

# -400("请求错误") 是个两头堵的码, 实测:
#     nav 接口上    -> cookie 坏了, 要重新扫码
#     acc/info 上   -> 传进去的参数不对(比如把整条主页链接当 UID 传进去)
# 所以必须**按接口**解释, 不能一把抓。nav 把它当"登录没了"是对的,
# acc/info 把它当"请求参数错"才对 —— 以前混在一起, 于是"参数传错"被报成
# "登录已失效", 用户白跑一趟去扫码。见 code_meaning 的 what 参数。
CODE_BAD_REQUEST = -400


def code_meaning(code, what="接口"):
    """返回码 -> 人话.

    `what` 说明这是**哪个**接口的返回 —— 同一个码在不同接口含义不同,
    尤其是 -400(见上面的注释)。
    """
    if code in RATE_LIMIT_CODES:
        return "被 B 站限流了(返回码 %s)" % code
    if code == CODE_BAD_REQUEST:
        if what == "登录检查":
            return "登录已失效(返回码 -400: 请求错误)"
        return ("%s拒绝了这次请求(返回码 -400: 请求错误) —— "
                "多半是传进去的 UID/参数不对" % what)
    if code in CODE_LOGIN_DEAD:
        return "登录已失效(返回码 %s)" % code
    if code in CODE_NOT_FOUND:
        return "确实不存在(返回码 %s)" % code
    return "接口返回异常(返回码 %s)" % code


def classify_code(code, message=""):
    """接口返回码 -> 一句人话(给用户看的原因).

    为什么需要它: 以前凡是 code != 0 都返回 None, 上层一律说"未找到该UP主" ——
    于是**被限流**、**登录失效**、**网络抖动**全被说成"没有这个人",
    用户会以为链接错了, 甚至以为号没了。这个区分必须做出来。

    返回 (类别, 人话):
        ok           成功
        rate_limited 被限流/请求过于频繁 —— 等一下再试
        login_dead   登录失效 —— 要重新扫码
        not_found    确实不存在(号被删/封, 或者拼错了)
        unknown      其它(把原始码和服务器原话带上)

    注意这里对 -400 一律按"参数/请求错"处理(不是登录失效), 因为调用它的
    地方主要是查 UP主/视频信息; 登录检查走的是 login_verdict/probe_login,
    那边 -400 才算登录失效。
    """
    if code == 0:
        return "ok", ""
    if code is None:
        return "unknown", "网络请求失败(%s)" % (clean_text(message or "") or "无响应")
    if code in RATE_LIMIT_CODES:
        return "rate_limited", ("被 B 站限流了(返回码 %s: %s)"
                               % (code, clean_text(message or "") or "请求过于频繁"))
    if code in CODE_LOGIN_DEAD:
        return "login_dead", ("登录已失效(返回码 %s: %s)"
                             % (code, clean_text(message or "") or "未登录"))
    if code == CODE_BAD_REQUEST:
        return "unknown", ("请求被拒绝(返回码 -400: %s) —— "
                          "多半是参数不对(比如 UID 位置放了整条链接)"
                          % (clean_text(message or "") or "请求错误"))
    if code in CODE_NOT_FOUND:
        return "not_found", ("确实不存在(返回码 %s: %s)"
                            % (code, clean_text(message or "") or "查无此人"))
    return "unknown", ("接口返回异常(返回码 %s: %s)"
                       % (code, clean_text(message or "") or "无说明"))

# B站 wbi 签名用的固定置换表
MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

PAGE_SIZE = 50            # 投稿列表每页条数
# 单次最多翻多少页. 50 页(2500 条)会截断投稿上万的 UP主(实测有一个 4017 条的号),
# 所以放宽到 400 页(20000 条); 翻页 1 页 1 秒, 真有这么大的号也只是慢一点。
MAX_PAGES = 400
HEAD_MAX_PAGES = 5        # 增量扫描最多往前翻 5 页(250 条), 再找不到基线就退回全量

COLL_PAGE_SIZE = 30
COLL_MAX_PAGES = 400      # 单个合集最多翻 400 页(12000 条), 防接口异常时死循环


# ---------------- 会话与登录 ----------------

def load_cookie():
    """从 BBDown.data 读 cookie(BBDown 自己也是读这个文件)."""
    try:
        with open(paths.cookie_file(), "r", encoding="utf-8-sig",
                  errors="ignore") as f:
            return f.read().strip()
    except OSError:
        return ""


def make_session():
    """建一个带着 cookie 的会话; 没有 requests 时返回 None."""
    if requests is None:
        return None
    session = requests.Session()
    session.headers.update(HEADERS)
    cookie = load_cookie()
    if cookie:
        session.headers["Cookie"] = cookie
    return session


_session = None


def api_session():
    """进程内共用的会话(探测充电专属/播放地址时用)."""
    global _session
    if _session is None:
        _session = make_session()
    return _session


def reset_session():
    """丢掉共用会话(测试或换了 cookie 之后)."""
    global _session
    _session = None


def _as_obj(data):
    """接口返回的 JSON 得是对象才算数.

    代理/门户页面/降级的网关可能回一个数组或一个字符串, 那时候 data.get()
    直接 AttributeError —— 这种错会越过所有 except Exception(有些调用点只
    包住了网络请求), 一路把守护带崩。
    """
    return data if isinstance(data, dict) else {}


def check_login(session):
    """nav 接口说"已登录"才算数.

    注意: 想给用户**解释为什么**不行(限流/真过期/网络), 用 login_verdict()。
    这个薄包装只是给"只要一个是/否"的地方用的。
    """
    return login_verdict(session)[0]


def login_verdict(session):
    """检查登录, 并说清"没登录"到底是哪种情况.

    返回 (是否已登录, 原因):
        (True,  "")               正常
        (False, "被 B 站限流了…")   等一下再试就行
        (False, "登录已失效…")      要重新扫码
        (False, "网络请求失败…")    网络问题
        (False, "接口返回异常…")    其它

    为什么不能只返回 True/False: 网络抖一下、或者撞上 -799 限流, 以前都会
    被说成"当前未登录或登录已失效, 请扫码登录" —— 用户会白跑一趟去扫码,
    而其实登录好得很(SESSDATA 还有大半年有效期)。这个区分必须做出来。
    """
    try:
        data = _as_obj(session.get(API_NAV, timeout=15).json())
    except Exception as e:
        return False, "网络请求失败(%s)" % clean_text(str(e) or "连接不上")
    code = data.get("code")
    payload = data.get("data") or {}
    if code == 0 and payload.get("isLogin"):
        return True, ""
    if code == 0 and not payload.get("isLogin"):
        return False, "登录已失效(cookie 已过期或被登出)"
    _kind, why = classify_code(code, data.get("message"))
    return False, why


def probe_login():
    """探测登录状态, 区分"真过期"和"被风控".

    返回 (结论, 说明):
        ok           已登录
        ratelimited  被风控/请求过于频繁(继续休息是对的, 不要让人去扫码)
        dead         登录确实失效了, 需要人工扫码
        network      网络/依赖问题
    """
    session = make_session()
    if session is None:
        return "network", "没有 requests 模块"
    try:
        data = _as_obj(session.get(API_NAV, timeout=15).json())
    except Exception as e:
        return "network", "请求异常: %s" % e
    code = data.get("code")
    is_login = bool((data.get("data") or {}).get("isLogin"))
    message = clean_text(data.get("message") or "")
    if code == 0 and is_login:
        return "ok", "已登录"
    if code in CODE_RATE_LIMITED:
        return "ratelimited", "code=%s %s" % (code, message)
    if code in CODE_LOGIN_DEAD:
        return "dead", "code=%s %s" % (code, message)
    if code == CODE_BAD_REQUEST:
        # 在 **nav** 这个接口上 -400 就是 cookie 坏了(实测: 登录正常时返回 0,
        # cookie 不合法时返回 "请求错误")。注意这和查 UP主信息时不是一回事 ——
        # 那边传错参数也返 -400, 所以 classify_code 把 -400 归为"参数错"。
        return "dead", "code=-400 请求错误 (nav 认为 cookie 不合法)"
    if code == 0 and not is_login:
        return "dead", "code=0 但 isLogin=false (cookie 已失效)"
    return "ratelimited", "code=%s %s" % (code, message)


def sessdata_expiry():
    """从 cookie 里的 SESSDATA 读出过期时间(本地判断, 不发请求)."""
    cookie = load_cookie()
    m = re.search(r"SESSDATA=([^;]+)", cookie)
    if not m:
        return None
    try:
        value = urllib.parse.unquote(m.group(1))
        timestamp = int(value.split(",")[1])
        return datetime.datetime.fromtimestamp(timestamp)
    except Exception:
        return None


# ---------------- wbi 签名 ----------------

def mixin_key(session):
    """从 nav 接口取 wbi 密钥; 取不到返回 None."""
    try:
        data = _as_obj(session.get(API_NAV, timeout=15).json())
    except Exception:
        return None
    wbi = (data.get("data") or {}).get("wbi_img") or {}
    if not isinstance(wbi, dict):
        return None
    # 键在、值是 null 的情况真出现过: basename(None) 会 TypeError
    img = os.path.splitext(os.path.basename(wbi.get("img_url") or ""))[0]
    sub = os.path.splitext(os.path.basename(wbi.get("sub_url") or ""))[0]
    raw = img + sub
    if not raw:
        return None
    return "".join(raw[i] for i in MIXIN_TAB)[:32]


def require_mixin_key(session):
    """管理器建任务时必须拿到密钥, 拿不到就没法继续."""
    key = mixin_key(session)
    if not key:
        raise RuntimeError("获取签名密钥失败")
    return key


def wbi_sign(params, key):
    """给参数加上 wts 和 w_rid 签名."""
    params = dict(params)
    params["wts"] = int(time.time())
    params = {k: re.sub(r"[!'()*]", "", str(v)) for k, v in params.items()}
    query = urllib.parse.urlencode(sorted(params.items()))
    params["w_rid"] = hashlib.md5((query + key).encode("utf-8")).hexdigest()
    return params


def api_get(session, url, params, key, retries=5):
    """带 wbi 签名的 GET; 被限流会退避重试.

    以前只对 -799 重试 3 次、每次固定等 3 秒。实测这个太弱: 账号在被限流时
    **连续十几次**都返回 -799, 三次重试等于没有, 上层就会拿到一个非 0 码,
    然后(在"加新UP主"那条路上)被说成"没有这个人" —— 见 classify_code。
    现在: 所有"请求太频繁"类返回码都重试, 间隔指数退避(3/6/12/24 秒)。
    """
    delay = 3
    last = None
    for attempt in range(retries):
        signed = wbi_sign(params, key)
        try:
            data = _as_obj(session.get(url, params=signed, timeout=15).json())
        except Exception as e:
            last = {"code": None, "message": str(e)}
            if attempt < retries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 30)
            continue
        code = data.get("code")
        if code == 0 or code not in RATE_LIMIT_CODES:
            return data
        last = data
        if attempt < retries - 1:
            log("接口返回 %s(%s), %d 秒后重试(%d/%d)"
                % (code, clean_text(data.get("message") or "") or "请求过于频繁",
                   delay, attempt + 1, retries))
            time.sleep(delay)
            delay = min(delay * 2, 30)
    # 重试完还是限流: 把最后一次的返回交给调用方, 由它翻译成人话
    return last if last else {"code": None, "message": "无响应"}


def api_get_plain(session, url, params, retries=4):
    """不带签名的接口(合集/系列); 偶发 -504(服务超时)/-799 会退避重试."""
    delay = 3
    last = None
    for i in range(retries):
        try:
            data = _as_obj(session.get(url, params=params, timeout=20).json())
        except Exception as e:
            data = {"code": None, "message": str(e)}
        if data.get("code") == 0:
            return data
        last = data
        if i < retries - 1:
            # 服务器返回的文字也可能夹带控制字符, 进日志前先洗一遍
            log("接口返回 %s(%s), %d 秒后重试"
                % (data.get("code"), clean_text(data.get("message") or ""),
                   delay))
            time.sleep(delay)
            delay = min(delay * 2, 30)
    raise RuntimeError("接口请求失败: %s"
                       % (clean_text(last.get("message") or "")
                          if last else "无响应"))


# ---------------- 链接识别 ----------------

def extract_mid(text):
    """从主页链接里取 UID; 纯数字也认."""
    m = re.search(r"space\.bilibili\.com/(\d+)", text or "")
    if m:
        return m.group(1)
    text = (text or "").strip()
    return text if text.isdigit() else None


def extract_video_id(text):
    """取 BV 号或 av 号."""
    m = re.search(r"BV[0-9A-Za-z]{10}", text or "")
    if m:
        return m.group(0)
    m = re.search(r"av(\d+)", text or "", re.IGNORECASE)
    return m.group(1) if m else None


# 只认真正的 b23.tv 域名. 以前用子串判断("b23.tv" in text), 于是
# https://b23.tv.evil.com/ 这种域名也会被当成短链 —— 解析短链是要发请求的,
# 而请求带着登录 Cookie, 发错域名等于把账号凭据送给对方。
_SHORT_LINK_HOSTS = ("b23.tv", "www.b23.tv")


def normalized_url(text):
    """补上协议头 -> 可以直接交给 requests 的地址.

    日常粘贴的写法("b23.tv/xxx")没有协议头, requests 拿到会直接报
    MissingSchema。判断"这是不是短链"和"要去请求哪个地址"必须是同一个字符串,
    否则会出现"校验通过、请求却发不出去"的假死。
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    if "://" not in raw[:12]:
        raw = "https://" + raw
    return raw


def host_of(text):
    """文本 -> 主机名(小写); 认不出主机名返回 "".

    查的是 hostname 而不是 netloc, 所以 "https://b23.tv@evil.com/" 这种
    userinfo 伪装会正确地解析成 evil.com, 带端口/大写也都能正确归一。
    """
    try:
        return (urllib.parse.urlsplit(normalized_url(text)).hostname or "").lower()
    except ValueError:
        return ""


def classify_link(text):
    """'up' / 'video' / None."""
    if re.search(r"space\.bilibili\.com/(\d+)", text or ""):
        return "up"
    if host_of(text) in _SHORT_LINK_HOSTS or extract_video_id(text):
        return "video"
    return None


def parse_collection_link(text):
    """认出合集/系列链接, 返回 {类型, mid, id}; 认不出来返回 None.

    纯字符串处理(不发请求), 所以能离线测试。支持的写法:
        space.bilibili.com/<mid>/lists/<号>?type=season|series     (新版)
        space.bilibili.com/<mid>/channel/collectiondetail?sid=<号> (老版合集)
        space.bilibili.com/<mid>/channel/seriesdetail?sid=<号>     (老版系列)
    """
    text = (text or "").strip()
    if not text:
        return None
    query = _query_of(text)
    m = re.search(r"space\.bilibili\.com/(\d+)/lists/(\d+)", text)
    if m:
        kind = "系列" if query.get("type") == "series" else "合集"
        return {"类型": kind, "mid": m.group(1), "id": m.group(2)}
    m = re.search(r"space\.bilibili\.com/(\d+)/channel/collectiondetail", text)
    if m and query.get("sid", "").isdigit():
        return {"类型": "合集", "mid": m.group(1), "id": query["sid"]}
    m = re.search(r"space\.bilibili\.com/(\d+)/channel/seriesdetail", text)
    if m and query.get("sid", "").isdigit():
        return {"类型": "系列", "mid": m.group(1), "id": query["sid"]}
    return None


def _query_of(url):
    """URL 的查询串 -> {参数: 单值}; 解析不出来给 {}.

    类型和 sid 都从这里精确取值: 以前用 "type=series" in text 这种子串判断,
    链接里别的参数带上同样的字样就会被认成系列, 于是走错接口。
    """
    try:
        query = urllib.parse.urlsplit(str(url or "")).query
        return {k: v[0] for k, v in urllib.parse.parse_qs(query).items() if v}
    except ValueError:
        return {}


def _safe_fetch(session, url):
    """请求一个非 B 站的地址(短链)时, 绝不带登录 Cookie.

    Cookie 是挂在 session.headers 上的裸头, requests 不按域名过滤它; 这里
    按请求把它摘掉(headers 里给 None = 这一条请求不带), 会话本身不受影响,
    后面调 B 站接口照样带 Cookie。没有 headers 的假会话(测试用)走原路。
    """
    if getattr(session, "headers", None) is None:
        return session.get(url, allow_redirects=True, timeout=15).url
    return session.get(url, allow_redirects=True, timeout=15,
                       headers={"Cookie": None}).url


def resolve_short_link(session, text):
    """b23.tv 短链 -> 真实地址; 其它原样返回.

    只有主机名确实等于 b23.tv / www.b23.tv 才发请求(而且不发 Cookie);
    别的文本一个字节都不发出去, 原样返回给调用方用正则自己认。
    """
    if host_of(text) not in _SHORT_LINK_HOSTS:
        return text
    try:
        return _safe_fetch(session, normalized_url(text))
    except Exception:
        return text


# ---------------- UP主信息 ----------------

def get_up_info(session, key, mid):
    """UP主名字.

    返回 (名字, 失败原因):
        成功        -> (名字, "")
        不存在的号  -> (None, "确实不存在(...)")
        被限流      -> (None, "被 B 站限流了(...)")
        登录失效    -> (None, "登录已失效(...)")
        其它        -> (None, "接口返回异常(...)")

    为什么要返回原因而不是简单的 None: 调用方要能把"没有这个人"和
    "现在被限流了"分开说。以前混成一句, 用户会以为链接错了。
    """
    data = api_get(session, "https://api.bilibili.com/x/space/wbi/acc/info",
                   {"mid": mid}, key)
    code = data.get("code")
    if code != 0:
        _kind, why = classify_code(code, data.get("message"))
        return None, why
    name = clean_text((data.get("data") or {}).get("name") or "")
    if not name:
        return None, "接口没返回名字(可能是隐藏/注销的账号)"
    return name, ""


def resolve_video(session, text):
    """视频链接 -> {aid, bvid, title}; 认不出来返回 None."""
    url = resolve_short_link(session, text)
    vid = extract_video_id(url)
    if not vid:
        return None
    params = {"bvid": vid} if vid.startswith("BV") else {"aid": vid}
    try:
        data = session.get(API_VIEW, params=params, timeout=15).json()
    except Exception:
        return None
    if data.get("code") != 0:
        return None
    info = data.get("data") or {}
    if not info.get("aid"):
        return None
    return {
        "aid": str(info["aid"]),
        "bvid": clean_text(info.get("bvid", "")),
        "title": clean_text(info.get("title", "")),
    }


# ---------------- 投稿列表 ----------------

def parse_page_items(data):
    """从 arc/search 的响应里取出 (本页视频列表, 投稿总数)."""
    payload = data.get("data") or {}
    vlist = (payload.get("list") or {}).get("vlist") or []
    items = []
    for v in vlist:
        if v.get("aid"):
            items.append({
                "aid": str(v["aid"]),
                "bvid": clean_text(v.get("bvid", "")),
                "title": clean_text(v.get("title", "")),
            })
    return items, to_int((payload.get("page") or {}).get("count"), 0)


def fetch_videos_page(session, key, mid, pn, ps=PAGE_SIZE):
    """拉一页投稿(按发布时间倒序). 返回 (本页视频, 投稿总数)."""
    data = api_get(session, API_SPACE_ARC,
                   {"mid": mid, "ps": ps, "pn": pn,
                    "order": "pubdate", "platform": "web"}, key)
    if data.get("code") != 0:
        raise RuntimeError("获取投稿列表失败: %s" % data)
    return parse_page_items(data)


def fetch_videos(session, key, mid, start_page=1, prefix=None):
    """完整拉取该 UP主 的全部投稿(按发布时间倒序).

    start_page/prefix 用于"前面几页已经拉过了"时接着往下拉, 不重复请求。
    返回 (videos, count, complete):
        count    = 接口报告的投稿总数
        complete = 列表是否可信地完整(接口提前返回空页或被翻页上限截断时为 False)
    """
    videos = list(prefix or [])
    pn = start_page
    count = 0
    complete = True
    while True:
        if pn > MAX_PAGES:
            complete = False
            log("警告: 投稿数超过 %d 条, 已到翻页上限, 本次列表按不完整处理"
                % (MAX_PAGES * PAGE_SIZE))
            break
        items, count = fetch_videos_page(session, key, mid, pn)
        videos.extend(items)
        if not items:
            # 空页: 只有"接口说本来就没有投稿"才算正常
            if count > 0:
                complete = False
                log("警告: 第 %d 页返回空, 但接口报告共 %d 条, 本次列表按不完整处理"
                    % (pn, count))
            elif pn > 1:
                complete = False
                log("警告: 第 %d 页返回空且接口没给出投稿总数, 本次列表按不完整处理"
                    % pn)
            break
        if count > 0:
            done = pn * PAGE_SIZE >= count
        else:
            # 没有总数信息时只能靠"这一页满没满"判断; 但缺了总数就无法确认
            # 列表完整, 一律按不完整处理(下轮会重新完整校验)
            done = len(items) < PAGE_SIZE
            complete = False
        if done:
            break
        pn += 1
        time.sleep(1)
    return videos, count, complete


def probe_latest(session, key, mid):
    """极轻量探测: 只拉最新一页.

    返回 (最新aid, 投稿总数, 本页视频列表)。一页(50 条)和一条的请求次数完全
    一样, 但多出来的内容能用来核对"最前面这几十条有没有变化"。
    """
    items, count = fetch_videos_page(session, key, mid, 1)
    return (items[0]["aid"] if items else None), count, items


def page_matches_prefix(page_aids, head_len, stored):
    """接口这一页里"头部新增之后"的视频, 是否都能在本地列表前缀里找到.

    只看成员不看顺序, 避免极少数"同一秒发布"造成的顺序抖动。对不上说明列表
    中部有变化(比如下架一个老视频同时重新公开另一个), 必须完整复核。
    """
    tail = page_aids[head_len:]
    if not tail:
        return True
    if len(stored) < len(tail):
        return False
    # 用 .get: 老状态文件里可能有缺 aid 的条目, 直接 [] 会 KeyError。
    # 缺 aid 就算"对不上" -> 走完整复核, 方向是安全的。
    return {v.get("aid") for v in stored[:len(tail)]} == set(tail)


def merge_head(head, stored):
    """把新增视频放到本地完整列表前面, 按 aid 去重并保持原顺序."""
    seen = set()
    merged = []
    for v in list(head) + list(stored):
        aid = v.get("aid")
        if not aid or aid in seen:
            continue
        seen.add(aid)
        merged.append(v)
    return merged


class SyncResult(object):
    """一次投稿列表同步的结果.

    mode:
        none        列表没有变化, 直接沿用本地记录(只花了 1 次请求)
        incremental 只拉了最前面几页, 已合并进本地完整列表
        full        完整拉取, 重建基线
    """

    __slots__ = ("videos", "meta", "mode")

    def __init__(self, videos, meta, mode):
        self.videos = videos
        self.meta = meta
        self.mode = mode

    def describe(self, stored_count):
        if self.mode == "none":
            return "投稿列表无变化, 已跳过拉取 (共 %d 个)" % len(self.videos)
        if self.mode == "incremental":
            return ("投稿列表已增量更新: 新增 %d 个, 合并后共 %d 个"
                    % (len(self.videos) - stored_count, len(self.videos)))
        return "投稿列表已更新: %d 个 (完整拉取)" % len(self.videos)


def sync_videos(session, key, mid, st, full_days=7, force_full=False,
                need_full=False):
    """同步该 UP主 的投稿列表, 能少发请求就少发, 但绝不为了快而漏掉视频.

    返回 SyncResult。判断依据(全部满足才走增量, 否则一律全量):
      · 接口最新一条 == 本地基线的最新一条, 且投稿总数相等 -> 认为无变化
      · 最新一条变了、总数增加了 -> 只做头部增量
      · 最新一条没变但总数变了 / 总数减少 -> 变化在列表中间, 必须全量

    强制全量的理由由调用方通过 force_full / need_full 传入:
      force_full  --full / --backfill / --retry-skip 这类"必须看全列表"的操作
      need_full   跳过/失败里有视频不在本地列表里(列表和记录脱节了)
    """
    from .state import (M_COMPLETE, M_LAST_AID, M_MODE, M_TOTAL)

    stored = st.videos
    meta = dict(st.meta or {})
    now = datetime.datetime.now()

    stored_aids = {v.get("aid") for v in stored if v.get("aid")}
    known = set(stored_aids)
    known.update(st.record)
    known.update(st.skip)
    known.update(st.failed)
    # 跳过/失败里的视频将来还要重试, 它们必须能在本地列表里找到
    missing_pending = [aid for aid in list(st.skip) + list(st.failed)
                       if aid not in stored_aids]

    def build_full_meta(videos, count, complete=True):
        return {
            M_LAST_AID: videos[0]["aid"] if videos else None,
            M_TOTAL: count,
            M_COMPLETE: bool(complete),
            "全量时间": now.strftime("%Y-%m-%d %H:%M:%S"),
            M_MODE: "全量",
        }

    def do_full(why):
        if why:
            log("投稿列表改为完整拉取(%s)" % why)
        videos, count, complete = fetch_videos(session, key, mid)
        return SyncResult(videos, build_full_meta(videos, count, complete), "full")

    # 1) 先判断有没有必须全量的理由
    why_full = []
    if force_full:
        why_full.append(force_full if isinstance(force_full, str)
                        else "指定了强制完整校验")
    if need_full:
        why_full.append(need_full if isinstance(need_full, str)
                        else "--backfill/--retry-skip 需要完整列表")
    if missing_pending:
        why_full.append("跳过/失败里有 %d 个视频不在本地投稿列表里"
                        % len(missing_pending))
    if not stored:
        why_full.append("本地还没有完整列表")
    elif meta.get(M_COMPLETE) is not True:
        why_full.append("本地列表缺少完整标记")
    else:
        last_full = st.full_time()
        if last_full is None:
            why_full.append("缺少上次完整校验时间")
        elif full_days <= 0:
            # 0 = 每次都完整校验, 等价于升级前的行为
            why_full.append("设置为每次都完整校验")
        elif (now - last_full) > datetime.timedelta(days=full_days):
            why_full.append("已超过 %d 天没做完整校验" % full_days)
    if why_full:
        return do_full("; ".join(why_full))

    # 2) 基线自检
    prev_aid = meta.get(M_LAST_AID)
    prev_count = st.api_count()
    if not prev_aid or prev_count is None or prev_count <= 0:
        return do_full("本地基线信息不完整")
    if stored[0].get("aid") != prev_aid:
        return do_full("本地基线与投稿列表不一致")

    # 3) 一次请求探测最新一条和总数
    try:
        top_aid, count, probe_items = probe_latest(session, key, mid)
    except Exception as e:
        return do_full("探测最新投稿失败: %s" % e)
    if top_aid is None:
        return do_full("接口没有返回任何投稿")
    top_aids = [v["aid"] for v in probe_items]
    if top_aid == prev_aid and count == prev_count:
        if not page_matches_prefix(top_aids, 0, stored):
            return do_full("最新一条和总数没变, 但最前面几十条对不上")
        return SyncResult(list(stored), meta, "none")
    if top_aid == prev_aid:
        # 顶部没变但总数变了: 改动在列表中间, 头部看不出来
        return do_full("最新投稿没变但投稿总数变化")
    expected = count - prev_count
    if expected <= 0:
        return do_full("投稿总数变化异常(减少或不变)")

    # 4) 只往前翻, 直到碰到一个以前见过的视频
    head = []
    fetched = []
    first_page_aids = []
    found = False
    exhausted = False
    last_page = 0
    last_count = 0
    prefetched = {1: (probe_items, count)}   # 探测那一页直接复用, 不重复请求
    pn = 1
    try:
        while pn <= HEAD_MAX_PAGES:
            if pn in prefetched:
                items, _page_count = prefetched.pop(pn)
            else:
                items, _page_count = fetch_videos_page(session, key, mid, pn)
            last_page = pn
            last_count = _page_count
            fetched.extend(items)
            if pn == 1:
                first_page_aids = [v["aid"] for v in items]
            if not items:
                exhausted = True
                break
            hit = False
            for item in items:
                if item["aid"] in known:
                    hit = True
                    break
                head.append(item)
            if hit:
                found = True
                break
            if len(fetched) >= expected + PAGE_SIZE:
                break
            pn += 1
            time.sleep(1)
    except Exception as e:
        return do_full("增量拉取失败: %s" % e)

    if not found:
        # 翻了半天还是没碰到见过的视频(新增太多或列表异常) -> 直接把剩下的拉完
        if exhausted:
            videos = fetched
            if last_count > 0:
                complete = last_page * PAGE_SIZE >= last_count
            else:
                complete = False
            if not complete:
                log("警告: 第 %d 页提前返回空, 本次列表按不完整处理" % last_page)
        else:
            log("增量扫描 %d 页仍未碰到已知视频, 继续把剩余列表拉完" % last_page)
            videos, count, complete = fetch_videos(
                session, key, mid, start_page=last_page + 1, prefix=fetched)
        return SyncResult(videos, build_full_meta(videos, count, complete), "full")

    if len(head) != expected:
        return do_full("本次新增 %d 个, 与投稿总数变化 %d 对不上"
                       % (len(head), expected))
    if not page_matches_prefix(first_page_aids, len(head), stored):
        return do_full("本次新增之外, 最前面几十条还对不上")

    videos = merge_head(head, stored)
    new_meta = dict(meta)
    new_meta.update({
        M_LAST_AID: videos[0]["aid"],
        M_TOTAL: count,
        M_COMPLETE: True,
        M_MODE: "增量",
    })
    return SyncResult(videos, new_meta, "incremental")


# ---------------- 合集 / 系列 ----------------
#
# 合集(season)和系列(series)都是 UP主 自己整理出来的一组视频。为什么值得单独支持:
# 合集里的视频和「投稿列表」大体重合但不完全重合 —— 联合投稿/合作视频、动态视频
# 这些不进投稿接口, 只有合集接口能看到。

def parse_archive_items(items):
    """合集/系列接口的条目 -> 和投稿列表一样的 {aid, bvid, title}."""
    out = []
    for v in items or []:
        aid = v.get("aid")
        if not aid:
            continue
        title = clean_text(v.get("title") or "").strip()
        out.append({
            "aid": str(aid),
            "bvid": clean_text(v.get("bvid", "")),
            "title": title or ("av%s" % aid),
        })
    return out


def fetch_season(session, mid, season_id):
    """拉一个合集的全部视频. 返回 (合集名, videos)."""
    videos = []
    name = ""
    pn = 1
    while True:
        if pn > COLL_MAX_PAGES:
            log("警告: 合集页数超过 %d, 先按已取到的 %d 个处理"
                % (COLL_MAX_PAGES, len(videos)))
            break
        data = api_get_plain(session, API_SEASON_ARCHIVES, {
            "mid": mid, "season_id": season_id,
            "page_num": pn, "page_size": COLL_PAGE_SIZE,
            "sort_reverse": "false",
        })
        payload = data.get("data") or {}
        meta = payload.get("meta") or {}
        if not name:
            name = clean_text(meta.get("title") or meta.get("name") or "").strip()
        items = payload.get("archives") or []
        videos.extend(parse_archive_items(items))
        page = payload.get("page") or {}
        total = to_int(page.get("total") or meta.get("total"), 0) or 0
        size = to_int(page.get("page_size"), COLL_PAGE_SIZE) or COLL_PAGE_SIZE
        if not items:
            if total > len(videos):
                log("警告: 合集第 %d 页提前返回空(接口说有 %d 个, 只拿到 %d 个)"
                    % (pn, total, len(videos)))
            break
        if total and pn * size >= total:
            break
        if not total and len(items) < COLL_PAGE_SIZE:
            break
        pn += 1
        time.sleep(0.5)
    return name, videos


def fetch_series(session, mid, series_id):
    """拉一个系列的全部视频. 返回 (系列名, videos)."""
    videos = []
    pn = 1
    while True:
        if pn > COLL_MAX_PAGES:
            log("警告: 系列页数超过 %d, 先按已取到的 %d 个处理"
                % (COLL_MAX_PAGES, len(videos)))
            break
        data = api_get_plain(session, API_SERIES_ARCHIVES, {
            "mid": mid, "series_id": series_id, "pn": pn,
            "ps": COLL_PAGE_SIZE, "only_normal": "true", "sort": "asc",
        })
        payload = data.get("data") or {}
        items = payload.get("archives") or []
        videos.extend(parse_archive_items(items))
        page = payload.get("page") or {}
        total = to_int(page.get("total"), 0) or 0
        size = to_int(page.get("size"), COLL_PAGE_SIZE) or COLL_PAGE_SIZE
        if not items:
            if total > len(videos):
                log("警告: 系列第 %d 页提前返回空(接口说有 %d 个, 只拿到 %d 个)"
                    % (pn, total, len(videos)))
            break
        if total and pn * size >= total:
            break
        if not total and len(items) < COLL_PAGE_SIZE:
            break
        pn += 1
        time.sleep(0.5)
    # 系列接口不返回名字, 名字要另外问一次(失败了就用系列号兜底)
    name = ""
    try:
        data = api_get_plain(session, API_SERIES_META,
                             {"mid": mid, "series_id": series_id})
        meta = (data.get("data") or {}).get("meta") or {}
        name = clean_text(meta.get("name") or "").strip()
    except Exception as e:
        log("取系列名失败(不影响下载): %s" % e)
    return name, videos


def fetch_collection(session, kind, mid, coll_id):
    """按类型拉合集/系列. 返回 (名称, videos)."""
    if kind == "系列":
        return fetch_series(session, mid, coll_id)
    return fetch_season(session, mid, coll_id)


def resolve_collection(session, text):
    """链接 -> 完整信息(类型/名称/视频列表). 认不出来返回 None."""
    spec = parse_collection_link(resolve_short_link(session, text))
    if not spec:
        return None
    name, videos = fetch_collection(session, spec["类型"], spec["mid"], spec["id"])
    spec["名称"] = name or ("%s%s" % (spec["类型"], spec["id"]))
    spec["视频列表"] = videos
    return spec


def collection_folder(spec):
    """合集/系列的存放目录: videos\\合集下载\\<名字>_<编号>\\. """
    from .util import sanitize_name

    leaf = "%s_%s" % (sanitize_name(spec["名称"]), spec["id"])
    if spec["类型"] == "系列":
        leaf = "%s_系列_%s" % (sanitize_name(spec["名称"]), spec["id"])
    return os.path.join(paths.collection_dir(), leaf)


# ---------------- 能不能下(充电/付费专属) ----------------

_unplayable_cache = {}


def clear_unplayable_cache():
    _unplayable_cache.clear()


def video_unplayable(aid):
    """这个视频是不是"要充电/要付费才能看".

    这类永远下不了, 不该算成"被限流", 否则守护会白等一轮又一轮。
    判据: 充电专属且当前账号不能播, 或者 UGC 付费试看。
    """
    if aid in _unplayable_cache:
        return _unplayable_cache[aid]
    session = api_session()
    if session is None:
        return False
    try:
        data = session.get(API_VIEW, params={"aid": aid}, timeout=15).json()
        info = data.get("data") or {}
        exclusive = info.get("is_upower_exclusive") is True
        can_play = info.get("is_upower_play") is True
        preview = info.get("is_ugc_pay_preview") is True
        unplayable = (exclusive and not can_play) or preview
    except Exception:
        return False
    _unplayable_cache[aid] = unplayable
    return unplayable


def playurl_code(aid, cid=None):
    """用 BBDown 同款接口(wbi playurl)看现在的返回码. 0=能下, 87008=被拦."""
    session = api_session()
    if session is None:
        return None
    try:
        if not cid:
            data = session.get(API_VIEW, params={"aid": aid}, timeout=15).json()
            cid = (data.get("data") or {}).get("cid")
        if not cid:
            return None
        key = mixin_key(session)
        if not key:
            return None
        params = wbi_sign({
            "avid": aid, "cid": cid, "fnval": 4048, "fnver": 0,
            "fourk": 1, "otype": "json", "qn": 0,
        }, key)
        data = session.get(API_PLAYURL, params=params, timeout=15).json()
        return data.get("code")
    except Exception:
        return None


def playurl_ok(aid, cid=None):
    """True = 和 BBDown 一样的接口现在能过."""
    return playurl_code(aid, cid) == 0


def playurl_media_url(aid, cid=None):
    """取一个真正能下载的媒体流地址(带 cookie 的 playurl 结果).

    两种形态都要认 —— 实测这个接口按 `fnval`/`qn` 的不同会返回其中之一:
        dash.video[].baseUrl   分片流(新版接口常见)
        durl[].url             整段 MP4 直链(qn=0 时实测返回这个)
    以前只认 dash, 于是"接口明明返回了地址"却被当成拿不到(探测误报失败)。
    """
    session = api_session()
    if session is None:
        return None
    try:
        if not cid:
            data = session.get(API_VIEW, params={"aid": aid}, timeout=15).json()
            cid = (data.get("data") or {}).get("cid")
        if not cid:
            return None
        key = mixin_key(session)
        if not key:
            return None
        params = wbi_sign({
            "avid": aid, "cid": cid, "fnval": 4048, "fnver": 0,
            "fourk": 1, "otype": "json", "qn": 0,
        }, key)
        data = session.get(API_PLAYURL, params=params, timeout=15).json()
        if data.get("code") != 0:
            return None
        inner = data.get("data") or {}
        dash = inner.get("dash") or {}
        for stream in (dash.get("video") or []):
            for key_name in ("baseUrl", "base_url"):
                if stream.get(key_name):
                    return stream[key_name]
            for url in (stream.get("backupUrl") or stream.get("backup_url") or []):
                if url:
                    return url
        for item in (inner.get("durl") or []):
            for key_name in ("url", "backup_url"):
                value = item.get(key_name)
                if isinstance(value, list):
                    value = value[0] if value else None
                if value:
                    return value
    except Exception:
        return None
    return None


def download_path_ok(aid, cid=None, bytes_wanted=131072):
    """**真的下一小段**看看下载通道通不通. 返回 (是否通, 说明).

    为什么不能只调一次 playurl 就当作"恢复了"(这是踩过的坑):
        守护判"被限流"之后会去探测, 拿到 code==0 就认为放行、1 分钟就重开。
        但 code==0 只说明**接口**给了地址, 不代表**CDN** 肯给数据。实测就这样
        空转了 76 分钟: 探测每次都是 0, 而每个视频都下不动。
        所以这里多走一步 —— 拿地址去真取一小段(默认 128KB), 用 HTTP 状态码
        和实际拿到的字节数说话。

    只取一小段: 够判断"通不通"就行, 别拿它当下载(那会平白多占带宽)。
    """
    session = api_session()
    if session is None:
        return False, "没有 requests 模块"
    url = playurl_media_url(aid, cid)
    if not url:
        code = playurl_code(aid, cid)
        return False, "拿不到播放地址(接口返回码 %s)" % code
    try:
        resp = session.get(url, headers={"Range": "bytes=0-%d" % (bytes_wanted - 1)},
                           timeout=20, stream=True)
    except Exception as e:
        return False, "连接 CDN 失败(%s)" % clean_text(str(e) or "网络错误")
    try:
        status = getattr(resp, "status_code", None)
        if status not in (200, 206):
            why = "CDN 拒绝(%s)" % status
            if status in (403, 401):
                why += " —— 多半还在风控"
            elif status == 412:
                why += " —— 请求过于频繁"
            return False, why
        got = 0
        for chunk in resp.iter_content(65536):
            got += len(chunk or b"")
            if got >= bytes_wanted:
                break
        if got <= 0:
            return False, "CDN 连通但一个字节都没给"
        return True, "已实际取到 %d 字节" % got
    except Exception as e:
        return False, "读取 CDN 数据失败(%s)" % clean_text(str(e) or "中断")
    finally:
        try:
            resp.close()
        except Exception:
            pass


# ---------------- 诊断输出(守护的 --probe-* 用) ----------------

def describe_code(code):
    """返回码 -> 一句人话."""
    if code == 0:
        return "正常"
    if code == 87008:
        return "被风控拦截(限流), 继续休息是对的"
    if code in CODE_RATE_LIMITED:
        return "请求过于频繁/风控, 不是登录过期"
    if code == CODE_CHARGING_EXCLUSIVE:
        return "充电/会员专属, 永远下不了"
    return "未知返回码"


def probe_api(sample_aid=None, out=print):
    """诊断: nav / view / playurl 三个接口现在分别是什么状态."""
    session = make_session()
    if session is None:
        out("没有 requests 模块")
        return
    cookie = load_cookie()
    names = [p.split("=", 1)[0].strip() for p in cookie.split(";") if "=" in p]
    out("cookie 里的字段: %s" % ", ".join(names))
    for need in ("SESSDATA", "bili_jct", "buvid3", "buvid4", "b_nut", "DedeUserID"):
        out("  %-10s %s" % (need, "有" if need in names else "没有"))

    try:
        data = session.get(API_NAV, timeout=15).json()
        out("nav     : code=%s isLogin=%s"
            % (data.get("code"), (data.get("data") or {}).get("isLogin")))
    except Exception as e:
        out("nav     : 请求异常 %s" % e)

    aid = sample_aid or "100000000000007"
    cid = None
    try:
        data = session.get(API_VIEW, params={"aid": aid}, timeout=15).json()
        out("view    : code=%s %s"
            % (data.get("code"), clean_text(data.get("message") or "")))
        info = data.get("data") or {}
        cid = info.get("cid")
        if not cid and info.get("pages"):
            cid = info["pages"][0].get("cid")
    except Exception as e:
        out("view    : 请求异常 %s" % e)
        return
    if not cid:
        out("playurl : 拿不到 cid, 跳过")
        return
    code = playurl_code(aid, cid)
    out("playurl : code=%s (%s)" % (code, describe_code(code)))


def probe_aids(aids, out=print):
    """看这些视频是不是充电/会员专属(这类永远下不下来, 不该算成限流)."""
    session = make_session()
    if session is None:
        out("没有 requests 模块")
        return
    for aid in aids:
        aid = (aid or "").strip()
        if not aid:
            continue
        try:
            data = session.get(API_VIEW, params={"aid": aid}, timeout=15).json()
        except Exception as e:
            out("%-16s 请求异常 %s" % (aid, e))
            continue
        info = data.get("data") or {}
        out("%-16s code=%s 充电专属=%s 能播=%s 付费=%s %s | %s"
            % (aid, data.get("code"), info.get("is_upower_exclusive"),
               info.get("is_upower_play"),
               (info.get("rights") or {}).get("pay"),
               clean_text(info.get("title", "")),
               clean_text((info.get("elec_high_level") or {}).get("sub_title")
                          or "")))
        time.sleep(0.4)
