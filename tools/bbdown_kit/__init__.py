# -*- coding: utf-8 -*-
"""BBDown 下载工具包 —— 管理器与守护共用的全部逻辑.

分层(上层只依赖下层, 不反向依赖):

    util        纯函数: 文件名清洗、时间/大小格式化、编码工具
    orders      处理顺序的取值与中文名
    config      读 下载设置.txt
    paths       所有路径的唯一来源(含老布局兼容)
    logging     日志、颜色、机器可读事件、管道解码
    state       下载状态.json 的唯一读写契约
    media       "本地是否已有这个文件"的唯一判断
    bilitools   B站接口: 登录/wbi/投稿列表/合集/播放地址探测
    procs       文件锁、进程树收尾、单实例互斥
    tasks       三份名单的盘点与任务排序
    analyze     缺口统计与自检报告(只读)
    maintenance 轮次之间的状态拨正(守护用)
    download    并发下载编排(从名单到文件落盘)
    lockstep    并发数与自适应降速
    runner      跑一轮: 起管理器 + 事件流统计 + 心跳 + 限流判定

两个入口很薄:
    tools/BBDown-manager.py   菜单 + 参数解析, 干活的都在 download/bilitools
    tools/下载守护.py          守护主循环, 干活的都在 runner/analyze/maintenance
"""

__version__ = "2.0"

__all__ = [
    "util", "orders", "config", "paths", "logging", "state", "media",
    "bilitools", "procs", "tasks", "analyze", "maintenance", "download",
    "lockstep", "runner",
]
