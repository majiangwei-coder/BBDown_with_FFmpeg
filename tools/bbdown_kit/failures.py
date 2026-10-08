# -*- coding: utf-8 -*-
"""下载失败的原因分类.

为什么需要它 —— 这是一个真实事故的教训:
    守护判定"被限流"的根据只有**连续失败几个视频**, 完全不看失败原因。
    09-25 实测出现过这样的鬼打墙, 整整 76 分钟:
        判定被限流(连续失败 4 个) -> 探测"接口返回码=0, 放行了" -> 1 分钟就重开
        -> 又是"成功 0, 失败 4" -> 再判定被限流 ...
    而当时真正在报的是:
        服务器可能并不支持多线程下载, 请使用 --multi-thread false 关闭多线程
        下载出现异常, 3秒后将进行自动重试...
    —— 这些是 CDN/视频本身的毛病, 跟账号被限流毫无关系。把它当限流处理,
    结果是并发被降到 1、每 40 秒掐一次进程, 越修越慢, 一个视频也没下成。

所以这里把"失败"分成几类, 让上层能区别对待:
    风控    被 B 站真拦了(有明确的返回码) —— 该休息
    限速    账号被限速(日志里直接这么说) —— 该休息
    多线程  服务器不吃多线程 —— 该关多线程重试, **不该休息**
    网络    连接抖动/超时, BBDown 自己会重试 —— 不该休息
    解析    BBDown 解析不出播放地址 —— 多半是视频本身的问题
    残缺    "Failed to download clip N" —— 流被截断, 重下有机会
    未知    没认出来

认不出来就返回"未知" —— 宁可不知道, 也不能瞎归类。
"""

import re

# 分类名(中文: 要直接进日志和 --status 给人看)
RATE_LIMIT = "风控"
THROTTLED = "限速"
MULTITHREAD = "多线程"
NETWORK = "网络"
PARSE = "解析"
NOFILE = "无成片"
CLIP = "残缺"
UNKNOWN = "未知"

# 只有这两类算"真的被拦", 值得守护停下整个下载去休息
REST_CAUSES = frozenset((RATE_LIMIT, THROTTLED))

# 具体原因 —— 一条信号一个正则, order 越小越优先。
#
# **order 是按"这一条的具体程度"排的, 不是按分类排的** —— 这点很重要:
# "解析分P失败" 和 "视频不存在(-404)" 都属于"解析"这个大类, 但后者具体得多
# (它直接告诉你要不要重试)。如果按分类排, 两者并列, 就会先报那个笼统的文案。
#
#   0  接口明说的返回码 —— 最权威。BBDown 打完 "解析此分P失败" 之后会跟一行
#      {"code":-404,...}, 那才是真正的原因(视频没了 / 要会员 / 被风控)。
#   0  风控码(87008)
#   1  账号级提示(限速/风控)
#   2  明确的权限/失效文案(比"解析失败"具体)
#   2  具体的技术原因(多线程/残缺)
#   3  解析相关(解析分P失败 / 拿不到信息 / 拿不到地址)
#   3  明确说"这流有问题, 升级试试"
#   4  取了流却没落盘
#   5+ 笼统的网络抖动 —— 排最后, 免得把上面那些具体的盖成"网络问题"
_SIGNALS = [
    ("payload", None, 0, re.compile(r'"?code"?\s*[:=]\s*(-?\d+)'),
     None),                      # 交给 _payload_meaning 翻译
    ("87008", RATE_LIMIT, 0, re.compile(r"\b87008\b"),
     "接口返回 87008(风控拦截)"),
    # BBDown 老版本/别的路径会直接打印 "返回码=-799" 这种文字
    ("barcode", RATE_LIMIT, 0, re.compile(r"返回码[=:\s]*(-?\d+)"),
     None),                      # 也交给 _payload_meaning 翻译
    ("throttle", THROTTLED, 1,
     re.compile(r"(请求过于频繁|访问过于频繁|操作太频繁|请稍后再试|"
                r"被限速|触发.*频控)"),
     "账号被限速({0})"),
    ("blocked", RATE_LIMIT, 1,
     re.compile(r"(风控|账号异常|拒绝访问|403\s*Forbidden)"),
     "风控/拒绝访问({0})"),
    ("vip", PARSE, 2,
     re.compile(r"(大会员|仅限.*(观看|会员)|需要.*会员|"
                r"该视频.*(不可|无法)播放|应版权方要求)"),
     "需要会员/版权限制({0})"),
    ("deleted", PARSE, 2,
     # 措辞纪律: 只说接口说了什么。"视频不存在"这种结论不能下 ——
     # 名单里能出现的视频, 解析时就说明它存在过(用户提醒的, 实测也对)。
     # (正则里留 "已被删除" 是因为接口可能真这么回, 那是**引用**, 不是我们的结论)
     re.compile(r"(播放地址不存在|稿件不可见|已被删除|已失效|啥都木有)"),
     "播放接口说给不出这一路({0})"),
    ("multithread", MULTITHREAD, 2, re.compile(r"不支持多线程"),
     "服务器不吃多线程"),
    ("clip", CLIP, 2, re.compile(r"Failed to download clip", re.IGNORECASE),
     "视频流被截断(Failed to download clip)"),
    ("parse_p", PARSE, 3, re.compile(r"解析此分P失败"),
     "解析分P失败(BBDown 拿不到这一路的播放地址)"),
    ("parse_v", PARSE, 3, re.compile(r"(获取视频信息失败|视频信息获取失败)"),
     "拿不到视频信息"),
    ("parse_u", PARSE, 3, re.compile(r"(获取播放地址失败|解析播放地址失败)"),
     "拿不到播放地址"),
    ("outdated", PARSE, 3, re.compile(r"请尝试升级到最新版本"),
     "BBDown 自己建议升级(流有问题)"),
    ("http404", NOFILE, 4,
     re.compile(r"(404\s*Not\s*Found|HTTP\s*404|状态码\s*404)", re.IGNORECASE),
     "取流时 CDN 返回 404"),
    ("retry", NETWORK, 5, re.compile(r"下载出现异常"),
     "下载出现异常(BBDown 会自动重试)"),
    ("neterr", NETWORK, 6,
     re.compile(r"(net_http_client_execution_error|"
                r"Connection\s*(refused|reset|timed out)|"
                r"远程主机强迫关闭|无法解析主机)", re.IGNORECASE),
     "网络抖动"),
]

# BBDown 输出里那个 JSON 的 code -> 人话。这些是**权威原因**, 优先于所有文案。
#
# 措辞上有一条纪律: **只说接口告诉了我们什么, 不替它下结论**。
# 踩过的例子: -404 本来写成"视频不存在", 但同一个视频的 view 接口返回 code=0
# (标题、分P、时长都正常), 只是播放接口给不出流 —— 说"不存在"是错的,
# 而且会让人以为名单脏了、跑去清名单。实测那次失败里 25 个抽查有 22 个
# 当时就能正常下, 更说明"不存在"这个结论站不住。
_PAYLOAD_CODES = {
    # 播放接口说自己没有这一路的流。具体是被删/被转番剧/地区限制, 接口不告诉我们,
    # 所以这里也不猜 —— 只把"谁说的、说了什么"记清楚。
    -404: "播放接口给不出流(-404 啥都木有)",
    -403: "没有访问权限(风控或受限)",
    -101: "未登录(接口说账号未登录)",
    -10403: "充电/会员专属, 下不了",
    -352: "被风控拦截",
    -412: "被风控拦截",
    -799: "请求过于频繁(被限速)",
    -509: "请求过于频繁(被限速)",
    -401: "请求被拒绝(可能要登录)",
    62002: "稿件不可见(这个码来自稿件接口, 不是播放接口)",
    62004: "稿件审核中",
    87008: "接口返回 87008(风控拦截)",
}
# 这些码算"真的被拦", 报出来要让守护休息
_PAYLOAD_REST_CODES = frozenset((-352, -412, -799, -509, -401, 87008))


def _payload_meaning(code_text):
    """BBDown 输出里那个 JSON 的 code -> (分类, 说明); 认不出返回 None."""
    try:
        code = int(code_text)
    except (TypeError, ValueError):
        return None
    if code == 0:
        return None                 # code:0 是正常返回, 不是失败信号
    why = _PAYLOAD_CODES.get(code)
    if why is None:
        why = "接口返回 %s" % code
    if code in _PAYLOAD_REST_CODES or code in _RATE_LIMIT_CODES:
        return RATE_LIMIT, why
    if code == -10403:
        return PARSE, why
    return PARSE, why


def code_kind(code):
    """接口返回码 -> (分类, 说明); 正常返回/认不出给 None.

    和"从文字里认原因"的分工: 文字只有 BBDown 愿意说的那部分, 而它经常只说一句
    "解析此分P失败(开启--debug查看详细信息)" —— 到底是视频没了、要充电、还是账号
    被风控, 从这句话里**看不出来**。这时候调用方可以自己问一次接口(播放接口),
    把码交给这里翻译: -352/-412 → 风控, -404 → 解析, -10403 → 充电专属……

    实测教训(2026-10-08): 账号正被风控的那几分钟里, 56 个视频全报"解析此分P失败",
    而 BBDown 的输出当时是乱码, 于是原因被记成"无成片"、守护也没看出自己在被拦。
    """
    if code is None:
        return None
    return _payload_meaning(str(code))


def stream_verdict(code, has_stream):
    """播放接口的 (返回码, 有没有流) -> (分类, 说明); 正常返回 None.

    `code=0` **不代表能下**: 被风控挑战时接口返回 code=0, 但 data 里只有一个
    v_voucher(验证凭证)、一条流都没有(实测 2026-10-08 02:00 连问四个视频全这样)。
    BBDown 拿到这种响应就会报"解析此分P失败"。

    这里把它归到"解析"而不是"风控": 同一个窗口里 BBDown 靠redeem凭证**下成了**
    191 个视频 —— 说明账号不是完全被拦死, 只是有一批撞在挑战上。按"解析"记下来
    (原因里写清是验证凭证), 管理器那边本来就会连续失败降速, 下一轮再重试;
    真要是彻底被拦, 接口会直接给 -352/-412 那种码, 那条路照旧判风控休息。
    """
    if code is None:
        return None                 # 没探到: 不编结论
    if code == 0:
        if has_stream is False:
            return PARSE, "播放接口只给了验证凭证(v_voucher), 没给流(被风控挑战)"
        return None                 # 接口现在能给流: 上次失败多半是瞬时的
    return code_kind(code)

# BBDown 每行都带自己的时间戳 "[2026-10-02 20:38:25.057] - ", 分类前洗掉,
# 免得那些数字里的片段碰到上面某些正则(也省得日志看着乱)。
_NOISE = re.compile(r"\[\d{4}-\d{2}-\d{2}[\d:.\s]*\]\s*-?\s*")


def strip_noise(text):
    """洗掉 BBDown 输出里的时间戳前缀."""
    return _NOISE.sub("", str(text or ""))

# 返回码在这个集合里才算"真的被限流/风控"; 别的码(比如 -10403 充电专属、
# -404 视频没了)不算。和 bilitools 的 CODE_RATE_LIMITED / describe_code 保持一致。
_RATE_LIMIT_CODES = frozenset((-352, -412, -799, -509, -401, -403, 87008))


def order_signals(signals):
    """把若干条信号去重并按优先级排序(最该报的在最前).

    为什么要单独抽出来: 原因是**边收边认**的(见 download._Tail), 于是可能
    出现"先认出笼统的、后认出具体的"。如果只是按发现顺序排, 最后报出来的
    就是那个笼统的 —— 实测因此把"视频已失效(-404)"报成了"解析分P失败"。

    接受的信号可以是 (分类, 说明), 也可以是 (优先级, 分类, 说明) ——
    后者用于"优先级没法从静态表里查出来"的情况(比如接口返回的码)。
    """
    out = []
    seen = set()
    for item in signals:
        if len(item) == 3:
            prio, kind, note = item
        else:
            kind, note = item
            prio = _rank(kind, note)
        key = (kind, note)
        if key in seen:
            continue
        seen.add(key)
        out.append((prio, kind, note))
    out.sort(key=lambda item: item[0])
    return [(kind, note) for _p, kind, note in out]


def _rank(kind, note):
    """一条 (分类, 说明) 的优先级 —— 越小越具体、越该先报.

    **这只是兜底**: 正常路径上优先级是识别时就带好的(见 scan_ranked) ——
    接口返回的码优先级是 0, 光看说明是查不出来的。这个兜底是给"手里只有
    (分类, 说明) 的调用方"用的(比如把几批信号合起来排序时补个位)。

    查不到就返回 99(排最后), 并保持稳定排序 —— 所以宁可查不到, 也别乱猜。
    """
    best = None
    for _key, sig_kind, order, _pat, sig_note in _SIGNALS:
        if sig_kind != kind or not sig_note:
            continue
        if "{0}" in sig_note:
            # 带占位符的说明: 动态部分没法直接比, 用占位符前面的固定文字对
            head = sig_note.split("{0}")[0]
            if not head or not note.startswith(head):
                continue
        elif note != sig_note:
            continue
        if best is None or order < best:
            best = order
    return best if best is not None else 99


def classify(text):
    """从一段输出里认出失败原因, 返回 (分类, 说明).

    用 search 扫**整段文本**而不是逐行: BBDown 的报错常常跨行
    (比如 "返回码" 和数字被日志前缀拆开), 逐行会漏。
    """
    got = scan_all(text)
    if not got:
        return UNKNOWN, ""
    return pick(got)


def scan_ranked(text):
    """和 scan_all 一样, 但每条都带上优先级: [(优先级, 分类, 说明), ...].

    给"边收边认"的场景用(见 download._Tail): 信号是分批认出来的, 攒起来之后
    必须还能按优先级排 —— 而优先级**只能在这一步定**(接口返回的码优先级是 0,
    事后拿说明去静态表里查是查不出来的)。
    """
    text = strip_noise(text)
    if not text:
        return []
    out = []
    seen = set()
    for key, kind, order, pat, note in _SIGNALS:
        m = pat.search(text)
        if not m:
            continue
        if key in ("payload", "barcode"):
            # 接口自己给的码 —— 权威原因, 优先于一切文案, 所以给 order 0
            got = _payload_meaning(m.group(1))
            if got is None:
                continue            # code:0 之类, 不是失败信号
            kind, note = got
            order = 0
        elif "{0}" in (note or ""):
            note = note.format(m.group(0))
        if kind is None or kind in seen:
            continue                # 同一类只留最具体的那个
        seen.add(kind)
        out.append((order, kind, note))
    out.sort(key=lambda item: item[0])
    return out


def scan_all(text):
    """这段文字里出现的**所有**失败信号, 按优先级排好(最该报的在最前).

    返回 (分类, 说明) 的列表 —— 排序信息在内部用掉, 对外只给你能直接用的。

    为什么返回全部而不是一个: 一次下载的输出里可能同时有
    "服务器不支持多线程" + "下载出现异常" + "解析此分P失败" ——
    只留一个会把别的信息丢掉, 而排查时"到底发生过哪几件事"很重要。
    上层可以只取第一个(见 pick)。
    """
    return [(kind, note) for _order, kind, note in scan_ranked(text)]


def pick(signals):
    """从 scan_all 的结果里选"最该报的那个".

    规则: 优先"该休息"的那类(风控/限速) —— 只要出现过一次, 守护就必须知道
    (这是和守护的约定, 不能改)。否则取**排在最前面的**(也就是最具体的):
    接口明说的码 > 权限/失效 > 具体技术原因 > 笼统的网络抖动。
    """
    if not signals:
        return UNKNOWN, ""
    for kind, note in signals:
        if kind in REST_CAUSES:
            return kind, note
    return signals[0]


def scan(text):
    """整段输出的结论: (分类, 说明)."""
    return pick(scan_all(text))


def should_rest(kind):
    """这个失败原因值不值得让守护停下整个下载去休息?"""
    return kind in REST_CAUSES


def advice(kind):
    """给用户的处理建议(一句话); 没有就返回空串."""
    return {
        MULTITHREAD: "已在 tools\\BBDown.config 里关掉多线程(--multi-thread false); "
                     "若还出现就是服务器端限流, 不用管",
        PARSE: "多半是**临时的**(实测重试就能下成), 直接重跑; "
               "老是这样再考虑是番剧/付费/地区限制",
        NOFILE: "BBDown 说下完了但本地没成片: 先重试; 反复出现就开 --debug 看",
        CLIP: "流被截断, 下次重跑有机会下成",
        NETWORK: "网络抖动, BBDown 自己会重试, 不用管",
        RATE_LIMIT: "等一会儿再跑; 频繁出现就把并发调低",
        THROTTLED: "等一会儿再跑; 频繁出现就把并发调低",
    }.get(kind, "")
