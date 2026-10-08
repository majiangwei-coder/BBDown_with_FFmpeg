# -*- coding: utf-8 -*-
"""下载编排: 从"名单"到"文件落盘"的这一段.

流程(对投稿列表和合集列表完全一样, 所以只写一份):
    补记本地已有文件 -> 处理跳过/失败/--backfill -> 算待下载
      -> 确认 -> 并发下载(见 lockstep) -> 每下一个就落一次状态

「跳过」是唯一会永久丢东西的地方, 所以规则写死在这里, 不再散落各处:
    · 累计失败 2 次       -> 进「跳过」(下次要重试必须在菜单按 R 或加 --retry-skip)
    · 接口确认充电/付费专属 -> 直接进「跳过」(反正永远下不了)
    · 被限流打断的一轮    -> 由守护把这一轮新写进「跳过」的条目撤回
    · 接口正在风控挑战时  -> **不进**「跳过」(见 account_level_failure):
                            那是账号层面的事, 不该算到具体视频头上
"""

import datetime
import os
import re
import subprocess
import sys
import threading
import time

from . import bilitools, failures, lockstep, media, paths, procs, workdirs
from .logging import decode_line, emit, highlight, log
from .state import TIME_LOCAL_EXISTS, State
from .util import elapsed_text, now_str

# 单个视频最多下多久(分钟)。超时就结束它, 下次自动重试。
DEFAULT_DOWNLOAD_TIMEOUT_MIN = 30

# BBDown 的换行有 \r\n(\n 也有, 进度还是 \r 刷新的)
_LINE_END = re.compile(rb"\r\n|\r|\n")


def split_output(buf):
    """BBDown 的原始字节 -> (完整行的文本列表, 还没结束的尾巴).

    两个坑都在这里解决:

    · **编码不固定**: BBDown(.NET) 按控制台代码页写 —— cp936 的窗口写 GBK,
      `chcp 65001` 的窗口写 UTF-8。以前这里是 `encoding="utf-8",
      errors="replace"`, GBK 那半边全变成 U+FFFD: 实测一轮日志里 **6 万个
      替换字符**(BBDown 的每一行都成了乱码), 更要命的是**失败原因也跟着认不出来**
      —— 56 个"解析此分P失败"全被记成"无成片", 于是守护看不出自己正被风控,
      既不休息也不撤回这一轮写进「跳过」的条目。
      现在逐行认编码(先 UTF-8 再 GB18030, 见 logging.decode_line)。
    · **进度是 \\r 刷新的**: 二进制模式没有 universal newlines, 得自己按 \\r 切,
      否则一条进度条会攒成一大坨(以前文本模式会自动断开)。
    """
    parts = _LINE_END.split(buf)
    tail = parts.pop()          # 最后一段可能还没结束, 留着等下一次
    return [decode_line(part) for part in parts], tail


# ---------------- 跑一次 BBDown ----------------

class _Tail(object):
    """边转发 BBDown 的输出, 边把"失败原因"当场认出来记下.

    为什么要截获: 判定"是不是真被限流"必须看**失败原因**, 而原因只有
    BBDown 的 stdout 里有(比如 "服务器可能并不支持多线程下载")。以前它直接
    继承了父进程的 stdout, 谁也没留下它说过什么 —— 于是守护只能靠"连续失败
    4 个"这种计数瞎猜, 实测把 CDN 的毛病误判成限流, 空转了一个多小时。

    **为什么不能只看"最后 N 行"**(踩过):
        · 同一个文件夹里并发下载时, 几个 BBDown 共用一个 stdout 管道,
          它们的输出会交错着进来 —— 固定窗口很容易被别的视频的输出冲掉;
        · 原因也不一定在最后: 实测 "解析此分P失败" 之后还会打印几十行
          "清理临时文件/任务完成", 原因被顶出窗口就再也找不回来了。
    所以现在是**边收边认**: 每来一段就扫一次, 认到就记住, 不依赖窗口。
    仍然保留最后几行(留作排查, 也不占多少内存)。
    """

    MAX_LINES = 60

    def __init__(self, stream=None):
        self.stream = stream
        self.lines = []
        self.lock = threading.Lock()
        self.thread = None
        self._pending = ""          # 还没认出信号的那部分文字
        self._found = []            # 已经认出来的 (分类, 说明)

    def feed(self, chunk):
        if not chunk:
            return
        with self.lock:
            self.lines.append(chunk)
            if len(self.lines) > self.MAX_LINES:
                del self.lines[:len(self.lines) - self.MAX_LINES]
            self._note(chunk)
        if self.stream is not None:
            try:
                self.stream.write(chunk)
                self.stream.flush()
            except Exception:
                pass            # 控制台写不进去不能影响下载

    def _note(self, chunk):
        """把这一段里的失败信号记下来(在锁里调用).

        认到之后就把累积的文字清掉: 信号本身已经记住了, 没必要一直攒着
        (一个视频的输出可能有几万行, 攒着纯属浪费)。

        记的是 **(优先级, 分类, 说明)** —— 优先级由 failures 在识别时定好,
        不在这里猜: 接口返回的码是 order 0, 事后从静态表里查是查不出来的。
        """
        self._pending += failures.strip_noise(chunk)
        if len(self._pending) > 4000:
            self._pending = self._pending[-2000:]   # 没收住就别无限涨
        got = failures.scan_ranked(self._pending)
        if got:
            self._found.extend(got)
            self._pending = ""

    def _pump(self, pipe):
        """按行读 BBDown 的输出(字节), 认编码之后转发 + 认信号."""
        buf = b""
        try:
            while True:
                chunk = pipe.read(4096)
                if not chunk:
                    break
                if isinstance(chunk, str):      # 万一传进来的是文本管道
                    self.feed(chunk)
                    continue
                buf += chunk
                lines, buf = split_output(buf)
                for line in lines:
                    self.feed(line + "\n")
        except Exception:
            pass
        finally:
            if buf:
                self.feed(decode_line(buf))
            try:
                pipe.close()
            except Exception:
                pass

    def start(self, pipe):
        self.thread = threading.Thread(target=self._pump, args=(pipe,),
                                       daemon=True)
        self.thread.start()
        return self

    def join(self, timeout=5):
        if self.thread is not None:
            self.thread.join(timeout)

    def text(self):
        with self.lock:
            return "".join(self.lines)

    def signals(self):
        """这一路输出里认出来的所有失败信号(去重, **按优先级**排好).

        注意是"按优先级"不是"按出现顺序": 原因是边收边认的, 可能先认出笼统的
        ("解析此分P失败")、后认出具体的("视频不存在(-404)")。按出现顺序排的话
        最后报出来的就是那个笼统的 —— 实测踩过。
        """
        with self.lock:
            found = list(self._found)
            found.extend(failures.scan_ranked(self._pending))
        return failures.order_signals(found)

    def reason(self):
        """(分类, 说明) —— 这次下载失败的原因.

        优先"该休息"的那类(风控/限速): 只要出现过一次, 上层就该知道。
        这也是以前跟守护的约定, 不能改。
        """
        return failures.pick(self.signals())


def account_level_failure(aid, throttle=None):
    """这次失败是"账号层面"的(正在被限流/风控挑战), 还是这个视频本身有问题?

    两个判据, 任一成立就算账号层面 —— 这种失败**不该写进「跳过」**:

    · **管理器自己正在降速**(连续失败到了阈值 / 还在冷却里): 那是它自己的限流
      判定, 说明现在撞的是一堵墙, 不是某个视频的毛病。
    · **接口现在只给验证凭证**(返回码 0 但一条流都没有 = v_voucher 风控挑战)。

    为什么必须区别对待(实测 2026-10-08 凌晨): 挑战是一阵一阵来的, 一轮里能撞掉
    300 多个视频, 而**同一个窗口里 BBDown 还能下成一大半** —— 照"累计失败 2 次
    就进跳过"办, 这一轮过去就平白丢几百个视频。守护那边的撤回机制只在它自己
    判定"被限流"时才动, 这两种情况它都看不到(接口返回码一直是 0)。

    代价只是"晚一点再判它没救": 等这堵墙过去了, 真下不了的视频照样会在两次
    失败后进「跳过」(那时 throttle 恢复、接口也能给流)。探不到/没登录一律
    按老规矩走, 不去猜。
    """
    if throttle is not None and (throttle.backoff_level > 0
                                 or throttle.cooling_down()):
        return True
    if not bilitools.can_probe_api():
        return False
    try:
        code, has_stream = bilitools.playurl_probe(aid)
    except Exception:
        return False
    return code == 0 and has_stream is False


def run_bbdown(url, folder, extra_args, timeout=None, aid=None, sink=None):
    """跑一次 BBDown. timeout(秒) 到了还没下完就结束它.

    卡死的 BBDown 会一直占着并发槽把整轮拖住(实测真会卡: 40 分钟 CPU 一动不动),
    所以必须在外面兜一层超时。

    aid 只用于"掐断之后清掉它留下的 <aid> 临时目录"(见 workdirs 模块);
    不传就只是不清理, 不影响下载本身。
    sink 传一个 _Tail 进来就能在下载结束后问它"刚才报了什么错"。
    """
    cmd = [paths.bbdown_exe(), url, "--work-dir", folder]
    ffmpeg = paths.ffmpeg_exe()
    if os.path.exists(ffmpeg):
        # 传绝对路径, 避免 --work-dir 下相对路径找不到 ffmpeg
        cmd += ["--ffmpeg-path", ffmpeg]
    # 单P和多P都统一使用 标题_aid, 避免同标题视频互相覆盖
    cmd += ["-F", "<videoTitle>_<aid>"]
    cmd += ["-M", "<videoTitle>_<aid>/[P<pageNumberWithZero>]<pageTitle>"]
    cmd += list(extra_args or [])
    log("开始下载: %s" % url)
    # 用管道接住 BBDown 的输出: 一边照原样转发到控制台(守护靠它显示进度),
    # 一边留最后几行下来当"失败原因"。
    # 拿不到 stdout(某些替身/极端情况)就退回"让它自己往控制台写", 功能不受影响,
    # 只是这次没有失败原因可记 —— 绝不能因为记不了原因就不下载。
    # 注意读的是**字节**(bufsize=0): 编码逐行认, 不能在这里写死(见 split_output)。
    try:
        proc = subprocess.Popen(cmd, cwd=paths.PROJ, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, bufsize=0)
    except FileNotFoundError:
        log("找不到 BBDown.exe, 请确认它和本程序在同一目录")
        return 1
    except Exception as e:
        log("运行 BBDown 出错: %s" % e)
        return 1
    tail = None
    pipe = getattr(proc, "stdout", None)
    if pipe is not None and hasattr(pipe, "readline"):
        tail = _Tail(stream=sys.stdout).start(pipe)
        if sink is not None:
            sink.append(tail)
    try:
        return proc.wait(timeout=timeout) if timeout else proc.wait()
    except subprocess.TimeoutExpired:
        log("这个视频超过 %.0f 分钟还没下完, 先结束它(下次运行会自动重试)"
            % (timeout / 60.0))
        if _kill_process_tree(proc):
            _clean_workdir(folder, aid)
        return -9
    except BaseException:
        # Ctrl+C 或者别的意外: 不能一走了之 —— BBDown/ffmpeg 会留在后台继续
        # 下载(管理器都退出了还在占账号), 还留下半个文件
        if _kill_process_tree(proc):
            _clean_workdir(folder, aid)
        raise
    finally:
        if tail is not None:
            tail.join()


def _clean_workdir(folder, aid):
    """把这一次下载留下的 <aid> 临时目录清掉(规则见 bbdown_kit/workdirs.py).

    只在**确认进程已经死透**之后才调 —— 否则等于把正在下载的目录从底下抽走。
    BBDown 没有断点续传, 这些半成品下次重下时它自己也会先删掉, 留着只是
    白占磁盘(实测这么攒到过 634 个目录 / 90 GB)。清理失败绝不能让下载流程
    出岔子, 所以这里兜住所有异常。
    """
    if not aid:
        return
    try:
        workdirs.remove_after_kill(folder, aid)
    except Exception as e:
        log("清理临时目录出错(忽略): %r" % e)


def _kill_process_tree(proc):
    """把 BBDown 和它的子进程(ffmpeg)一起收掉. 返回"确认已经死了".

    返回值只用于决定"能不能立刻清它的工作目录": 没死透就绝不碰那个目录。
    """
    try:                    # /T 连它的子进程(ffmpeg)一起收掉
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True)
    except Exception:
        pass
    try:
        proc.wait(timeout=15)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        return proc.poll() is not None
    except Exception:
        return False


def download_one(folder, video, extra_args, dup_titles, timeout=None):
    """下一个视频: 下之前记快照, 下之后对比有没有真的多出文件.

    返回 (视频, 是否成功, 返回码, 是否新文件, 用时秒)。

    失败原因**挂在返回的那个 video 字典上**(键 "_失败原因"), 不塞进返回值里:
    元组形状是既有契约, 好几处调用点和测试都在按 5 项解包, 加一项会把它们
    全打断。守护要靠这个原因区分"真被限流"和"CDN 抽风"(见 failures 模块)。
    """
    aid = video["aid"]
    log("开始下载: %s [%s]" % (highlight(video["title"]), aid))
    emit("video_start", aid=aid, title=video["title"], folder=folder)
    started = time.time()
    before = media.MediaIndex(folder).snapshot()
    sink = []
    try:
        rc = run_bbdown("https://www.bilibili.com/video/av%s" % aid, folder,
                        extra_args, timeout=timeout, aid=aid, sink=sink)
    except TypeError as e:
        # 兼容不接受 sink 的旧实现(测试替身、用户自己改过的脚本)。
        # 没有原因可记不等于不能下载 —— 退回去照常跑。
        if "sink" not in str(e):
            raise
        rc = run_bbdown("https://www.bilibili.com/video/av%s" % aid, folder,
                        extra_args, timeout=timeout, aid=aid)
    after = media.MediaIndex(folder).snapshot()
    took = time.time() - started
    # 失败原因: 从 BBDown 的输出里认(见 failures 模块)。
    #
    # 为什么**不能**写成"只在 rc != 0 时才去认": 实测有一整类失败是
    #     BBDown 打印 "解析此分P失败", 然后照常打印 "任务完成", 退出码是 0,
    #     而本地根本没有成片。
    # 那时候 rc==0, 于是原因字段一直是空的 —— 一次真实事故里 1437 个失败
    # 全是空原因, 事后只能靠翻日志还原。现在改成"只要最终算失败就去认",
    # 和 rc 是多少无关。
    # (BBDown 输出里可能带它自己的时间戳, 分类前先洗掉, 免得干扰匹配)
    reason = ("", "")
    if sink:
        try:
            reason = sink[0].reason()
        except Exception:
            reason = ("", "")
    # 只看"这个视频自己的"新文件: 同一个文件夹里并行下载时, 别的视频落盘的
    # 文件以前会被算成它的成功(快照是整个文件夹的差分), 于是没下成的视频
    # 被记成"已下载", 再也不会重试。
    new_file = bool(media.new_video_files(before, after, aid, video["title"]))
    if rc == -9 and new_file:
        # -9 = 超过时限, 是我们自己把 BBDown/ffmpeg 掐掉的。这时候落盘的成片
        # 一定是半成品, 不能当成功(当成功就永久记成"已下载", 那个打不开的
        # 文件再也没人管)。顺手删掉它, 让下次重试从头下, 也免得 BBDown
        # 看到文件已存在就跳过。
        gone = media.discard_partial_output(folder, before, after, aid,
                                            video["title"])
        log("超时被结束, 已丢弃这次的半成品 %d 个, 下次重试" % len(gone))
        new_file = False
    # 兜底: BBDown 自己写出的名字因为清洗差异和我们的算法对不上时, 靠"本地
    # 已经有这个视频"补认。但只在 BBDown 说成功(rc==0)时才算 —— 否则并行下载
    # 时别人刚落盘的文件会把"没下成"顶成"下成了", 这个视频就再也不重试了。
    ok = new_file or (rc == 0 and media.video_file_exists(
        folder, aid, video["title"], dup_titles))
    # 第四处空白: rc==0、BBDown 也没报任何错, 但就是没有成片。
    # 这种情况日志里**一个字的原因都没有**, 只记"未知"等于没记 ——
    # 至少要说清"BBDown 说完成了, 但文件没落地", 排查方向完全不同
    # (不是网络/不是限流, 而是落盘或文件名对不上)。
    if not ok and reason[0] == failures.UNKNOWN and rc == 0:
        reason = (failures.NOFILE,
                  "BBDown 报完成(rc=0)但本地没成片(耗时 %.0f 秒)"
                  % took)
    emit("video_end", aid=aid, title=video["title"], folder=folder,
         ok=bool(ok), rc=rc, new_file=bool(new_file), timeout=(rc == -9),
         elapsed=int(took), reason=reason[0], reason_text=reason[1])
    # 把原因挂在 video 上带给调用方(见函数说明: 不改元组形状)
    video["_失败原因"] = reason[0] if not ok else ""
    video["_失败说明"] = reason[1] if not ok else ""
    return video, ok, rc, new_file, took


# ---------------- 一份名单的下载流程 ----------------

def download_video_list(folder, st, args, stats, who="该UP主",
                        throttle=None, ctx=None, videos=None):
    """把一份名单(投稿列表或合集列表)里还没下的视频下完.

    这是 process_up(按UP主) 和 process_collection(按合集) 共用的那一半。

    videos 给的是"这次要检查的那批视频", 默认取 st.videos(该名单的全部)。
    为什么不在这里自己 st.pending(): 调用方可能刚刚把一份新列表塞进
    st.videos, 而 step 2(retry_skip)已经把「跳过」里的条目放回队列了 ——
    这时候再按 st.pending() 过滤, 会把刚放回来的视频又排除掉。
    返回本次下载统计 {"downloaded": n, "failed": m}。
    """
    timeout = getattr(args, "download_timeout", None)

    # 1) 本地已有文件的直接补记(手工拷进来的、以前版本漏记的)
    index = media.MediaIndex(folder)
    recovered = media.reconcile_media(folder, st.videos, st.record, index)
    if recovered:
        for aid in recovered:
            st.skip.pop(aid, None)
            st.failed.pop(aid, None)
        log("根据本地文件自动补记 %d 个已下载视频" % len(recovered))
        st.save()

    # 2) 菜单『R』/ --retry-skip: 把「跳过」列表中的视频重新放回下载队列
    if getattr(args, "retry_skip", False) and st.skip:
        _retry_skipped(st)

    if st.failed:
        log("有 %d 个视频上次下载失败, 本次会自动重试" % len(st.failed))
    if st.skip:
        log("跳过列表中有 %d 个视频(如需重试, 请在菜单中选择『R』, "
            "或加 --retry-skip 参数)" % len(st.skip))

    dup_titles = st.duplicate_titles()
    local = media.MediaIndex(folder)

    # 3) --backfill: 移除"有已下载记录但本地没有实际文件"的记录, 重新下载补回内容
    if getattr(args, "backfill", False):
        removed = []
        keep = {}
        for aid, entry in st.record.items():
            video = st.video_of(aid)
            if video is not None and not local.has(aid, video["title"], dup_titles):
                removed.append(aid)
            else:
                keep[aid] = entry
        if removed:
            log("--backfill: 移除 %d 条无实际文件的已下载记录, 将重新下载补回内容"
                % len(removed))
            st.record = keep
            st.save()

    # 4) 算待下载
    current_aids = {v["aid"] for v in st.videos}
    new_videos = (
        [v for v in st.videos
         if v.get("aid") and v["aid"] not in st.record and v["aid"] not in st.skip]
        if videos is None else list(videos)
    )
    new_videos, present = _split_locally_present(st, new_videos, local, dup_titles)
    if present:
        log("有 %d 个视频本地已有同名文件, 直接标记为已下载" % present)
        st.save()
    deleted = [aid for aid in st.record if aid not in current_aids]

    print()
    log("当前共 %d 个视频, 已下载过 %d 个, 本次待下载 %d 个"
        % (len(st.videos), len(st.videos) - len(new_videos), len(new_videos)))
    if deleted:
        log("有 %d 个视频已从名单里消失(UP主删除/隐藏或移出合集), "
            "本地已下载的会保留不动" % len(deleted))

    if args.limit is not None and args.limit == 0:
        log("本次数量限制为 0, 仅刷新列表, 不下载")
        return {"downloaded": 0, "failed": 0}
    if not new_videos:
        print()
        log("没有需要下载的新视频")
        return {"downloaded": 0, "failed": 0}

    if not args.yes and ctx is not None:
        preview = [v["title"] for v in new_videos[:8]]
        if not ctx.confirm(folder, len(new_videos), preview):
            log("用户跳过, 未下载")
            return {"downloaded": 0, "failed": 0}

    if args.limit is not None:
        new_videos = new_videos[: args.limit]

    throttle = throttle or lockstep.Throttle(
        args.parallel, args.interval, args.fail_threshold, args.pause_seconds)
    print()
    log("开始下载 %d 个视频 (%s) ..." % (len(new_videos), throttle.describe()))
    emit("up_pending", folder=folder, total=len(st.videos),
         pending=len(new_videos))

    total_count = len(new_videos)
    failed_this_round = []

    def run_one(video):
        return download_one(folder, video, args.extra_args, dup_titles, timeout)

    def on_result(video, result, done, batch_elapsed):
        if result:
            video, ok, rc, _new_file, took = result
        else:
            ok, rc, took = False, None, 0.0
        # 原因由 download_one 挂在 video 上(成功时是空的)
        reason = (video.get("_失败原因") or "", video.get("_失败说明") or "")
        avg = batch_elapsed / done if done else 0.0
        if ok:
            st.mark_done(video["aid"], video["title"], video.get("bvid", ""),
                         when=now_str())
            st.save()
            log("(%d/%d) 完成 ✓ %s [用时 %s, 平均 %s/个]"
                % (done, total_count, highlight(video["title"], "green"),
                   elapsed_text(took), elapsed_text(avg)))
            return True
        failed_this_round.append((video["aid"], video["title"]))
        count = st.mark_failed(video["aid"], video["title"], when=now_str(),
                               reason=reason[1])
        # 这次失败是"账号层面"的吗(被限流/风控挑战)? 是的话踩一脚刹车 ——
        # 实测那种挑战按时间窗来(好一分钟坏几分钟), 坏窗口里继续撞几乎全是白撞。
        # 判据本身很便宜: 管理器已经在降速时直接返回, 否则才去问一次接口。
        blocked = account_level_failure(video["aid"], throttle)
        if blocked:
            pause = throttle.pause_for_block()
            log("撞上账号层面的限流/风控挑战(第 %d 次): 暂停 %s 再继续"
                % (throttle.block_level, elapsed_text(pause)))
        # 把原因也说出来(以前只有"失败"两个字, 事后完全查不出为什么)
        why_text = ""
        if reason[1]:
            why_text = " [%s: %s]" % (reason[0], reason[1])
            tip = failures.advice(reason[0])
            if tip:
                why_text += " -> %s" % tip
        if count >= 2 and blocked:
            # 账号正在被限流/风控挑战(接口只给验证凭证, 或管理器自己已经在降速):
            # 这不是这个视频的毛病, 不能让它进「跳过」—— 留在「失败」里,
            # 这堵墙过去了下一轮接着下。
            st.save()
            log("(%d/%d) 失败 ✗ %s (第%d次失败: 现在是账号层面的限流/风控挑战, "
                "先不进跳过列表, 下次重试) [用时 %s, 平均 %s/个]%s"
                % (done, total_count, highlight(video["title"], "red"), count,
                   elapsed_text(took), elapsed_text(avg), why_text))
            return False
        if count >= 2:
            st.move_failed_to_skip(video["aid"])
            st.save()
            emit("video_skipped", aid=video["aid"], title=video["title"],
                 folder=folder, reason="累计失败2次", rc=rc,
                 fail_kind=reason[0])
            log("(%d/%d) 失败 ✗ %s (累计失败2次, 已加入跳过列表) "
                "[用时 %s, 平均 %s/个]%s"
                % (done, total_count, highlight(video["title"], "red"),
                   elapsed_text(took), elapsed_text(avg), why_text))
        else:
            st.save()
            log("(%d/%d) 失败 ✗ %s (第%d次失败, 下次运行会自动重试) "
                "[用时 %s, 平均 %s/个]%s"
                % (done, total_count, highlight(video["title"], "red"),
                   count, elapsed_text(took), elapsed_text(avg), why_text))
        return False

    # should_stop: 用户要求"优雅停止"时, 不再开新任务, 但在下的下完为止。
    # 这样停下之后磁盘上不会留半成品, 也不用事后清理。
    ok_count, fail_count = lockstep.run_batch(
        new_videos, run_one, on_result, throttle,
        should_stop=procs.graceful_stop_requested)
    stats["downloaded"] += ok_count
    stats["failed"] += fail_count
    emit("up_end", folder=folder, total=len(st.videos),
         downloaded=ok_count, failed=fail_count)

    if failed_this_round:
        log("%s下载失败 %d 个(累计失败2次才会跳过, 否则下次自动重试):"
            % (who, len(failed_this_round)))
        for aid, title in failed_this_round:
            log("  - %s [%s]" % (highlight(title, "red"), aid))
    return {"downloaded": ok_count, "failed": fail_count}


def _split_locally_present(st, videos, local, dup_titles):
    """把"本地已有同名文件"的当成已下载, 返回 (剩下的, 补记了几个)."""
    present = 0
    remain = []
    for video in videos:
        if local.has(video["aid"], video["title"], dup_titles):
            st.record[video["aid"]] = {
                "title": video["title"],
                "bvid": video.get("bvid", ""),
                "time": TIME_LOCAL_EXISTS,
            }
            st.failed.pop(video["aid"], None)
            present += 1
        else:
            remain.append(video)
    return remain, present


def _failed_titles(st, videos):
    """本次跑过的视频里, 现在还在「失败」里的那些(收尾日志用)."""
    for video in videos:
        if video["aid"] in st.failed:
            yield video["aid"], video["title"]


def _retry_skipped(st):
    """把「跳过」里的视频放回队列, 顺便清掉和「已下载」重复的条目.

    只处理仍然存在于名单里的视频, 已被UP主删除的条目原样保留。
    """
    current = {v["aid"]: v for v in st.videos}
    retry = [aid for aid in st.skip if aid in current and aid not in st.record]
    stale = [aid for aid in st.skip if aid in st.record]
    for aid in retry + stale:
        st.skip.pop(aid, None)
    if retry or stale:
        st.save()
    if retry:
        # 手动重试是很明确的用户意图, 连"永久失败"名录里的标记一起忘掉。
        # 否则守护下一轮 prepare_round 又会照名录把它写回「跳过」——按了 R
        # 等于没按, 而这是用户唯一的自救入口(比如后来开通了充电/会员)。
        from . import maintenance
        gone = maintenance.forget_permanent(retry)
        if gone:
            log("已清掉 %d 个永久失败标记(这些视频下次会重新尝试)" % len(gone))
    if stale:
        log("已清理 %d 条与已下载记录重复的跳过条目" % len(stale))
    if retry:
        log("已把 %d 个已跳过的视频重新加入下载队列:" % len(retry))
        for aid in retry[:10]:
            log("  - %s [%s]" % (highlight(current[aid]["title"], "yellow"), aid))
        if len(retry) > 10:
            log("  ... 等共 %d 个" % len(retry))
    return retry, stale


def _frozen(folder):
    """这个文件夹(绝对路径)是不是在冻结名单里.

    冻结名单存的是"相对数据目录"的写法, 所以这里要转一下。转不出来
    (文件夹不在数据目录下)就当没冻结 —— 不能因为路径怪就拒绝干活。
    """
    from . import freeze

    try:
        rel = os.path.relpath(str(folder), paths.data_root())
    except ValueError:
        return False
    if rel.startswith(".."):
        return False
    return freeze.is_frozen(rel)


# ---------------- 按UP主 / 按合集 ----------------

def process_up(session, key, folder, mid, args, stats, ctx=None, throttle=None):
    """一个 UP主: 同步投稿列表 -> 下载没下过的."""
    # 冻结兜底: 任务列表已经剔掉冻结的名单了, 但万一还有别的调用点
    # (手工命令、老脚本)塞进来一个, 这里必须拒绝 —— 否则"冻结"就漏了。
    if _frozen(folder):
        log("跳过 %s: 这个名单已冻结(不更新、不下载)"
            % os.path.basename(str(folder).rstrip("\\/")))
        return
    os.makedirs(folder, exist_ok=True)
    log("处理UP主: %s" % highlight(folder))
    log("正在检查该UP主投稿更新...")
    emit("up_start", folder=folder, mid=mid)
    st = State.load(folder)
    stored_count = len(st.videos)
    try:
        result = bilitools.sync_videos(
            session, key, mid, st,
            full_days=getattr(args, "full_days", 7),
            force_full=_force_full_reason(args),
            need_full=None,
        )
    except Exception as e:
        log("获取投稿失败: %s" % e)
        emit("up_end", folder=folder, error=str(e), downloaded=0, failed=0)
        stats["failed"] += 1
        return
    if not result.videos:
        log("该UP主暂无公开投稿")
        emit("up_end", folder=folder, total=0, downloaded=0, failed=0)
        return
    if result.mode == "none":
        log(result.describe(stored_count))
    elif result.mode == "incremental":
        log("%s (保存在 %s)" % (result.describe(stored_count),
                              paths.STATE_NAME))
    else:
        log("%s (完整拉取, 保存在 %s)" % (result.describe(stored_count),
                                        paths.STATE_NAME))
    if result.mode != "none":
        st.videos = result.videos
        st.meta = result.meta
        st.save()
    return download_video_list(folder, st, args, stats, who="该UP主",
                               throttle=throttle, ctx=ctx)


def _force_full_reason(args):
    """哪些参数强制要求"必须看完整列表"."""
    reasons = []
    if getattr(args, "full", False):
        reasons.append("指定了 --full")
    if getattr(args, "backfill", False):
        reasons.append("--backfill 需要完整列表")
    if getattr(args, "retry_skip", False):
        reasons.append("--retry-skip 需要完整列表")
    return "; ".join(reasons)


def process_collection(session, spec, args, stats, folder=None, ctx=None,
                       throttle=None):
    """按合集/系列下载: 拉列表 -> 写状态 -> 走和投稿完全一样的下载流程."""
    folder = folder or bilitools.collection_folder(spec)
    if _frozen(folder):
        log("跳过 %s: 这个名单已冻结(不更新、不下载)"
            % os.path.basename(str(folder).rstrip("\\/")))
        return
    os.makedirs(folder, exist_ok=True)
    log("处理%s: %s" % (spec["类型"], highlight(folder)))
    emit("up_start", folder=folder, mid=spec["mid"])
    st = State.load(folder)
    stored = list(st.videos)
    videos = spec.get("视频列表")
    if videos is None:
        try:
            name, videos = bilitools.fetch_collection(
                session, spec["类型"], spec["mid"], spec["id"])
        except Exception as e:
            log("获取%s列表失败: %s" % (spec["类型"], e))
            emit("up_end", folder=folder, error=str(e), downloaded=0, failed=0)
            stats["failed"] += 1
            return
        if name:
            spec = dict(spec, 名称=name)
    if not videos:
        log("该%s里没有可下载的视频(可能是空的/已失效)" % spec["类型"])
        emit("up_end", folder=folder, total=0, downloaded=0, failed=0)
        return
    known = {v.get("aid") for v in stored}
    added = sum(1 for v in videos if v["aid"] not in known)
    if stored:
        log("%s列表已刷新: 共 %d 个 (上次 %d 个, 新增 %d 个, 保存在 %s)"
            % (spec["类型"], len(videos), len(stored), added, paths.STATE_NAME))
    else:
        log("%s列表: 共 %d 个 (保存在 %s)"
            % (spec["类型"], len(videos), paths.STATE_NAME))
    st.videos = videos
    st.meta = st.build_collection_meta(spec, len(videos))
    st.save()
    return download_video_list(folder, st, args, stats,
                               who="该%s" % spec["类型"], throttle=throttle,
                               ctx=ctx)


def process_single_folder(args, stats, folder=None, ctx=None, throttle=None):
    """把"单视频下载"里登记过的零散视频补一遍: 失败的重试, 文件丢了的重下."""
    folder = folder or paths.single_dir()
    if _frozen(folder):
        log("跳过 单视频下载: 这个名单已冻结(不更新、不下载)")
        return
    os.makedirs(folder, exist_ok=True)
    log("处理单视频清单: %s" % highlight(folder))
    emit("up_start", folder=folder, mid="")
    st = State.load(folder)
    _backfill_single_list(st)
    if not st.videos:
        log("单视频清单是空的, 跳过")
        emit("up_end", folder=folder, total=0, downloaded=0, failed=0)
        return
    if st.skip:
        log("单视频清单里有 %d 个已跳过(要重试就在菜单按 R 或加 --retry-skip)"
            % len(st.skip))
    return download_video_list(folder, st, args, stats, who="单视频",
                               throttle=throttle, ctx=ctx)


def _backfill_single_list(st):
    """老的单视频只写了「已下载」没写名单, 补进名单守护才能一起补."""
    known = {v.get("aid") for v in st.videos}
    added = 0
    for aid, entry in st.record.items():
        if aid in known:
            continue
        entry = entry or {}
        st.videos.append({"aid": aid, "bvid": entry.get("bvid", ""),
                          "title": entry.get("title", "")})
        added += 1
    if added:
        st.save()
    return added


def register_single_video(info):
    """把一个待下单视频登记进"单视频下载"名单.

    先登记再下载: 万一下载失败, 下次(或守护)还会自动补, 不会白点一次。
    """
    folder = paths.single_dir()
    os.makedirs(folder, exist_ok=True)
    st = State.load(folder)
    _backfill_single_list(st)
    st.add_to_list({"aid": info["aid"], "bvid": info["bvid"],
                    "title": info["title"]})
    st.save()
    return st


def download_single_now(st, info, args, stats):
    """下载一个"单视频下载"里的视频并记账. 返回是否成功."""
    folder = st.folder
    before = media.MediaIndex(folder).snapshot()
    rc = run_bbdown("https://www.bilibili.com/video/av%s" % info["aid"], folder,
                    args.extra_args,
                    timeout=getattr(args, "download_timeout", None),
                    aid=info["aid"])
    after = media.MediaIndex(folder).snapshot()
    # 和 download_one 用同一套判据: 只认"这个视频自己的"新成片, 不能再拿整个
    # 文件夹的差集(上一轮或并行的残留文件会被算成这一次的成功)
    new_file = bool(media.new_video_files(before, after, info["aid"],
                                          info["title"]))
    if rc == -9 and new_file:
        # 同 download_one: 被我们掐掉的下载留下的半成品不能当成功
        media.discard_partial_output(folder, before, after, info["aid"],
                                     info["title"])
        new_file = False
    if new_file or (rc == 0 and media.video_file_exists(
            folder, info["aid"], info["title"], st.duplicate_titles())):
        st.mark_done(info["aid"], info["title"], info.get("bvid", ""),
                     when=now_str())
        st.save()
        stats["downloaded"] += 1
        return True
    if not st.is_done(info["aid"]):
        st.mark_failed(info["aid"], info["title"], when=now_str())
    st.save()
    stats["failed"] += 1
    return False


# ---------------- 兼容旧调用点的小函数 ----------------

def snapshot_files(folder):
    return media.MediaIndex(folder).snapshot()


def is_successful_download(before, after):
    return media.is_successful_download(before, after)


def local_time_text():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
