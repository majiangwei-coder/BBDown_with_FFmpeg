# -*- coding: utf-8 -*-
"""并发下载 + 自适应降速.

这段逻辑以前埋在 download_video_list 的 while 循环里, 和"记状态、写日志、发事件"
混在一起, 出事时很难看出到底是"并发数算错了"还是"状态写错了"。现在它只干一件事:

    给定一批待下载的视频, 用当前的并发数/间隔把它们跑完, 并在连续失败时降速。

降速规则(和以前完全一致):
  · 连续失败 fail_threshold 个 -> 并发降为 1, 间隔至少 5 秒, 暂停一段时间
    (pause_seconds 逐次翻倍, 最多 30 分钟);
  · 撞上"账号被拦"(风控挑战/限流, 见 pause_for_block) -> 直接踩一脚刹车
    (60 秒起步, 翻倍最多 5 分钟);
  · 恢复正常后连续成功 5 个 -> 并发和间隔还原。
暂停期间如果还有任务在跑, 就继续收结果, 而不是干等着。
"""

import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from .logging import log
from .util import elapsed_text

MAX_PAUSE_SECONDS = 1800
# 撞上"账号被拦"时的刹车: 45 秒起步, 连着撞翻倍, 封顶 2 分钟。
# 为什么封顶比"连续失败降速"短这么多: 实测那种挑战是**短周期**的 ——
# 好的窗口只有一两分钟(每分钟能下成 30 个), 停太久会整个睡过去, 白丢一批;
# 而多试一次的代价只有 1 秒 + 几个请求。所以宁可多试, 不要睡过头。
BLOCK_PAUSE_SECONDS = 45
MAX_BLOCK_PAUSE = 120


class Throttle(object):
    """当前的并发/间隔状态, 以及连续成功/失败的计数."""

    __slots__ = ("max_parallel", "current_parallel", "base_interval", "interval",
                 "fail_threshold", "pause_seconds", "consecutive_fail",
                 "consecutive_ok", "backoff_level", "cooldown_until",
                 "block_until", "block_level")

    def __init__(self, parallel, interval, fail_threshold, pause_seconds):
        self.max_parallel = max(1, int(parallel))
        self.current_parallel = self.max_parallel
        self.base_interval = max(0.0, float(interval))
        self.interval = self.base_interval
        self.fail_threshold = max(1, int(fail_threshold))
        self.pause_seconds = max(0, int(pause_seconds))
        self.consecutive_fail = 0
        self.consecutive_ok = 0
        self.backoff_level = 0
        self.cooldown_until = 0.0   # 连续失败降速后的冷却(原来的机制)
        self.block_until = 0.0      # "账号被拦"刹车的截止时间(单独一份, 见下)
        self.block_level = 0        # 这个刹车连着踩了几次(成功一次就复位)

    def describe(self):
        return ("初始 %d 个并行, 间隔 %.1f 秒; 连续失败 %d 个会自动降速"
                % (self.max_parallel, self.base_interval, self.fail_threshold))

    def on_failure(self):
        """记一次失败; 到达阈值就降速并设置冷却. 返回是否刚触发降速."""
        self.consecutive_ok = 0
        self.consecutive_fail += 1
        # 这里必须问"现在是否正在冷却", 不能只看 cooldown_until 是不是非零:
        # 它一旦被设过就再没人清零(on_success 也不清), 于是第一次降速之后
        # on_failure 永远提前返回 False —— 暂停和降速一辈子只发生一次。
        if self.consecutive_fail < self.fail_threshold or self.cooling_down():
            return False
        self.current_parallel = 1
        self.interval = max(self.interval, 5.0)
        self.backoff_level += 1
        pause = min(self.pause_seconds * (2 ** (self.backoff_level - 1)),
                    MAX_PAUSE_SECONDS)
        self.cooldown_until = time.time() + pause
        return True

    def pause_for_block(self, seconds=None):
        """撞上"账号被拦"(风控挑战/限流)时踩一脚刹车. 返回这次停多少秒.

        为什么不能只是降速继续跑: 实测 2026-10-08 凌晨那几小时的失败是**按时间窗**
        来的(按分钟统计: 03:47~03:48 全成、03:50~03:52 全败、03:53~03:54 全成、
        03:55~03:58 全败……好窗口一两分钟、坏窗口三四分钟, 大约每 6 分钟一个来回)。
        坏窗口里 ~20 次尝试只成 0~5 个 —— 那些尝试纯属白撞, 还一直在给账号加压。
        所以撞上就停一会儿, 等风控散了再继续: 45 秒起步, 连着撞翻倍(封顶 2 分钟),
        下一次成功就松开(见 on_success)。

        seconds 默认取模块里的 BLOCK_PAUSE_SECONDS(测试会把它改成 0, 免得干等)。
        """
        if seconds is None:
            seconds = BLOCK_PAUSE_SECONDS
        self.block_level += 1
        pause = min(seconds * (2 ** (self.block_level - 1)), MAX_BLOCK_PAUSE)
        self.block_until = max(self.block_until, time.time() + pause)
        return pause

    def on_success(self):
        """记一次成功; 恢复得足够好就把并发/间隔还原. 返回是否刚恢复."""
        self.consecutive_fail = 0
        self.consecutive_ok += 1
        # 能下成了 = 那堵墙散了: 刹车立刻松开, 别抱着"最多 5 分钟"干等 ——
        # 好窗口只有一两分钟, 白等过去就白丢一批视频。
        # 注意**只松刹车**, 不动 cooldown_until: 那是连续失败降速的冷却,
        # 按原来的设计要等它自己到期(见 on_failure 里那段注释)。
        self.block_until = 0.0
        self.block_level = 0
        if (self.current_parallel < self.max_parallel
                or self.interval > self.base_interval) and self.consecutive_ok >= 5:
            self.current_parallel = self.max_parallel
            self.interval = self.base_interval
            self.backoff_level = 0
            return True
        return False

    def cooling_down(self):
        return time.time() < max(self.cooldown_until, self.block_until)

    def cooldown_left(self):
        return max(0.0, max(self.cooldown_until, self.block_until) - time.time())


def run_batch(pending, run_one, on_result, throttle, should_stop=None):
    """把 pending 里的视频按 throttle 的节奏跑完.

    run_one(video) -> 任意结果对象, 原样交给 on_result
    on_result(video, result) -> True 表示成功, False 表示失败
        (由调用方负责记状态/写日志; 返回值只用来驱动降速)
    should_stop() -> True 表示**别再开新任务了**, 但已经在下的一定要跑完

        "优雅停止"就是靠它: 用户按了停止之后, 手上这几个视频继续下到完整落盘,
        只是不再从 pending 里取新的。下完自然就退出了 —— 不留半成品、不留
        工作目录、不用事后清理。返回时 pending 里没轮到的那些原样留着,
        下次运行会重新排队。

    返回 (成功数, 失败数)。
    """
    pending = list(pending)
    ok_count = 0
    fail_count = 0
    batch_start = time.time()
    done = 0
    futures = {}
    stop_announced = False

    def submit_one(executor):
        video = pending.pop(0)
        futures[executor.submit(run_one, video)] = video

    def stop_now():
        """该不该停止取新任务(只问一次, 并说一声)."""
        nonlocal stop_announced
        if should_stop is None:
            return False
        try:
            if not should_stop():
                return False
        except Exception:
            return False
        if not stop_announced:
            stop_announced = True
            log("收到停止请求: 不再开新任务, 等在下的 %d 个下完就收工"
                % len(futures))
        return True

    def process_finished(finished):
        nonlocal done, ok_count, fail_count
        for future in finished:
            video = futures.pop(future)
            done += 1
            try:
                result = future.result()
            except Exception as e:
                # 只把 future.result() 包进来: 以前连第一次 on_result 也包在
                # 里面, 它自己一出错就会被当成"下载异常"再以 result=None 调
                # 第二遍, 同一个视频一轮里记两次失败, 提前越过永久跳过阈值。
                log("下载异常: %s: %s" % (video.get("title", ""), e))
                result = None
            ok = bool(on_result(video, result, done, time.time() - batch_start))
            if ok:
                ok_count += 1
                if throttle.on_success():
                    log("下载已恢复正常, 并发恢复为 %d, 间隔 %.1f 秒"
                        % (throttle.current_parallel, throttle.interval))
            else:
                fail_count += 1
                if throttle.on_failure():
                    log("连续失败 %d 个, 判定为被限流: 并发降为 1, 间隔 %.1f 秒, "
                        "暂停 %.0f 秒后自动继续(第%d次触发)"
                        % (throttle.consecutive_fail, throttle.interval,
                           max(0.0, throttle.cooldown_left()),
                           throttle.backoff_level))

    executor = ThreadPoolExecutor(max_workers=throttle.max_parallel)
    try:
        while futures or pending:
            if throttle.cooling_down():
                if not futures:
                    if stop_now():
                        break       # 没有在下的, 又被要求停: 直接收工
                    rest = throttle.cooldown_left()
                    log("暂停中, 剩余 %.0f 秒..." % rest)
                    time.sleep(min(rest, 5))
                    continue
                finished, _ = wait(list(futures), return_when=FIRST_COMPLETED)
                process_finished(finished)
                continue
            if futures:
                finished, _ = wait(list(futures), return_when=FIRST_COMPLETED)
                process_finished(finished)
            # 每次要开新任务之前问一句: 要求停就不再取新的了。
            # 已经在下的一定让它跑完 —— 这是"优雅停止"的全部要点。
            while len(futures) < throttle.current_parallel and pending:
                if stop_now():
                    break
                submit_one(executor)
                if throttle.interval > 0:
                    time.sleep(throttle.interval)
            if not futures:
                # 手上的都下完了: 要么没活干了, 要么被要求停 -> 都该退出
                # (这里必须判一下, 否则"被要求停 + 还有 pending"会空转)
                if not pending or stop_now():
                    break
    except KeyboardInterrupt:
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        executor.shutdown(wait=False)
    if stop_announced and pending:
        log("已按停止请求收工: 还有 %d 个没轮到, 下次运行会重新排队" % len(pending))
    return ok_count, fail_count


def average_text(elapsed, count):
    """平均每个用了多久, 拼成日志里的一段."""
    if not count:
        return ""
    return "平均 %s/个" % elapsed_text(elapsed / count)
