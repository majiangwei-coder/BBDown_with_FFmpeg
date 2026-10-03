# -*- coding: utf-8 -*-
"""「本地是不是已经有这个视频了」—— 所有判重逻辑的唯一实现.

以前守护和管理器各写了一份一模一样的判断, 两份代码只要有一处改动就会
"一边认为下过、一边认为没下", 于是同一个视频被反复重下或被永久漏掉。
现在两边都调这里。

BBDown 实际写出来的文件名(和 --file-pattern 一致):
    单P:  <标题>_<aid>.mp4
    多P:  <标题>_<aid>\\[P01]<分P标题>.mp4

判断顺序(为什么是这个顺序):

1. 精确匹配 `<标题>_<aid>` 的文件名或同名子目录 —— 对"选""腰"这种单字标题
   同样可靠, 所以必须最先看。
2. 标题长度 < 2 就直接结束, 不再做前缀模糊匹配 —— 否则标题"选"会把
   "选美大赛_999.mp4" 错认成自己, 于是这个视频永远下不下来。
3. 同名标题出现多次(dup_titles)时, 前缀匹配不可信, 直接结束。
4. 前缀匹配: 老格式(只有标题没 aid 后缀)的文件只能这么认, 但只认
   "标题后面紧跟边界字符(_ - 空格 [ ( .)或正好到结尾"的那种 —— 否则
   标题"预告"会把"预告片_123.mp4"认成自己, 这个视频就永远不下。
   而且只要名字尾巴上带着号(「标题_123」「标题 123」「标题 (123)」),
   那个号就得**等于自己的 aid**: 同一个 UP 下「猫咪」和「猫咪 日常」是
   两个视频, 后者的文件决不能被前者认领。
"""

import os

# 算"下载成功"的扩展名: 出现这些才算真的下到了东西
VIDEO_EXTS = {".mp4", ".flv", ".mkv", ".m4a"}
# 所有可能出现在文件夹里的媒体(含封面图, 判重时也算"有文件")
MEDIA_EXTS = VIDEO_EXTS | {".mp3", ".jpg", ".jpeg", ".webp", ".png"}
# 下到一半的临时文件: 不算成功
PARTIAL_EXTS = {".m4s", ".part", ".download", ".tmp"}


class MediaIndex(object):
    """一个文件夹里"有哪些媒体文件"的快照.

    一次扫描, 多处复用 —— 以前每个视频都重新 os.listdir 一遍, 大文件夹里
    几千个视频会扫几千次。扫完之后文件夹没变就可以一直用; 下载过程中
    有了新文件要重新建(见 refresh)。
    """

    __slots__ = ("folder", "names", "dirs")

    def __init__(self, folder):
        self.folder = folder
        self.names = set()     # 不带扩展名的文件名, 例如 "标题_123"
        self.dirs = {}         # 清洗后的子目录名 -> 真实名字
        self.refresh()

    def refresh(self):
        self.names = _scan_file_basenames(self.folder)
        self.dirs = _scan_subfolders(self.folder)
        return self

    # ---------- 判断 ----------

    def has(self, aid, title, dup_titles=frozenset()):
        """本地是否已有这个视频对应的文件."""
        t = _clean(title)
        exact = "%s_%s" % (t, aid)
        if exact in self.names:
            return True
        if exact in self.dirs and _dir_has_media(self.folder, self.dirs[exact]):
            return True
        # 太短的标题不再做前缀模糊匹配, 免得把别的视频误判成"已下载"
        if len(t) < 2:
            return False
        if t in dup_titles:
            return False
        if any(_prefix_hit(n, t, aid) for n in self.names):
            return True
        for clean, raw in self.dirs.items():
            if _prefix_hit(clean, t, aid) and _dir_has_media(self.folder, raw):
                return True
        return False

    def _dir_has_media(self, sub):
        return _dir_has_media(self.folder, sub)

    def snapshot(self):
        """文件夹里所有文件的相对路径集合(下载前后对比用)."""
        return _walk_files(self.folder)


def _clean(name):
    from .util import sanitize_name
    return sanitize_name(name)


# 前缀匹配的边界字符: 标题后面紧跟这些(或正好到结尾)才算同一个视频.
# "_" 是标准格式「标题_aid」的分隔符, 空格/[/./- 是历史上出现过的写法。
_PREFIX_BOUNDARY = frozenset("_-[(. ")
# 去掉分隔符用的一圈: 老名字可能是「标题 123」「标题 (123)」「标题.123」
_PREFIX_TRIM = "_-[(.)] "


def _prefix_hit(name, title, aid=None):
    """名字以标题开头、且标题后面紧跟边界字符(或正好结束)才算命中.

    尾巴带 aid 的名字(标准格式「标题_123」)只在那个数字就是自己的 aid 时
    才算命中。同一个 UP 下「猫咪」和「猫咪 日常」是两个视频: 后者的文件叫
    「猫咪 日常_222.mp4」, 以前它会被前者认领, 于是「猫咪」永远下不下来
    (记录里写着"已下载", 幽灵清理也同意"文件在", 谁也发现不了)。
    只有"根本没有 aid 后缀"的老格式文件(「标题 上集.mp4」)才走宽松的前缀
    匹配 —— 那种名字里没有号, 只能靠标题认。
    """
    if not name.startswith(title):
        return False
    rest = name[len(title):]
    if not rest:
        return True
    if rest[0] not in _PREFIX_BOUNDARY:
        return False
    core = rest.strip(_PREFIX_TRIM)
    if core.isdigit():
        # 整个尾巴就是个数字(「标题 123」「标题 (123)」「标题.123」): 那是文件
        # 主人的 aid, 不是自己就不能认
        return aid is not None and str(aid) == core
    tail = name.rsplit("_", 1)
    if len(tail) == 2 and tail[1].isdigit():
        # 名字以 _<数字> 结尾(「标题 日常_222」): 同理
        return aid is not None and str(aid) == tail[1]
    # 名字里根本没有号(「标题 上集」): 老文件只能靠标题认, 保持宽松
    return True


def _scan_file_basenames(folder):
    """{不带扩展名的文件名} —— 只收视频扩展名, 而且必须是真有内容的文件.

    0 字节的成片是"下载到一半被杀/断电"留下的残骸(真实库里就有), 它要是算
    数, 这个视频会被永久当成"已下载", 一直留着那个打不开的坏文件不再重试。
    """
    result = set()
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                base, ext = os.path.splitext(entry.name)
                if ext.lower() not in VIDEO_EXTS:
                    continue
                try:
                    if entry.is_file() and entry.stat().st_size > 0:
                        result.add(base)
                except OSError:
                    continue
    except OSError:
        pass
    return result


def _scan_subfolders(folder):
    """{清洗后的子目录名: 真实子目录名}.

    BBDown 下载多P视频时会建一个以"标题_aid"命名的子目录, 分P文件放在里面,
    所以判重必须把子目录也算上。
    """
    result = {}
    try:
        for name in os.listdir(folder):
            if os.path.isdir(os.path.join(folder, name)):
                result[_clean(name)] = name
    except OSError:
        pass
    return result


def _dir_has_media(folder, sub):
    """这个子目录(含更深层)里有没有视频文件."""
    try:
        for _root, _dirs, files in os.walk(os.path.join(folder, sub)):
            for name in files:
                if os.path.splitext(name)[1].lower() in VIDEO_EXTS:
                    return True
    except OSError:
        pass
    return False


def _walk_files(folder):
    """文件夹里所有文件的相对路径(用 / 分隔, 跨平台可比)."""
    result = set()
    try:
        for root, _dirs, files in os.walk(folder):
            for name in files:
                full = os.path.join(root, name)
                result.add(os.path.relpath(full, folder).replace(os.sep, "/"))
    except OSError:
        pass
    return result


# ---------------- 给不方便建索引的调用点用的便捷函数 ----------------

def media_basenames(folder):
    return _scan_file_basenames(folder)


def subfolders(folder):
    return _scan_subfolders(folder)


def dir_has_media(folder, sub):
    return _dir_has_media(folder, sub)


def video_file_exists(folder, aid, title, dup_titles, names=None, dirs=None):
    """文件夹里是否已有这个视频. names/dirs 可以预先算好(性能).

    签名和历史调用点保持一致, 内部走 MediaIndex 的同一套判断。
    """
    if names is not None and dirs is not None:
        index = MediaIndex.__new__(MediaIndex)
        index.folder = folder
        index.names = names
        index.dirs = dirs
        return index.has(aid, title, dup_titles)
    return MediaIndex(folder).has(aid, title, dup_titles)


def is_successful_download(before, after):
    """下载前后文件快照对比: 是不是真的多出来一个视频文件.

    只认视频/音频扩展名 —— 封面图 (.jpg) 不算成功, 以前踩过这个坑:
    下载失败但存了封面, 被当成"下完了"。
    """
    for name in after - before:
        ext = os.path.splitext(name)[1].lower()
        if ext in VIDEO_EXTS and ext not in PARTIAL_EXTS:
            return True
    return False


def new_video_files(before, after, aid, title, dir=None):
    """after - before 里确实属于这个视频的新文件.

    只看精确的「<标题>_<aid>」名字(单P是文件, 多P是子目录) —— 不能拿整个
    文件夹的差集当成功: 同一个文件夹里并行下载时, 别的视频落盘的新文件会
    被算到这个视频头上, 于是没下成的视频被记成"已下载"。dir 给的是相对路径
    的顶层(多P时新文件在 <标题>_<aid>/ 里), 默认看全部新增。
    """
    exact = "%s_%s" % (_clean(title), aid)
    hits = set()
    for name in after - before:
        base = name.split("/", 1)[0]
        if base != exact and os.path.splitext(base)[0] != exact:
            continue
        # 必须真的是成片: 封面 .jpg、弹幕 .xml、杀掉后留下的无扩展名残骸
        # 都叫这个名字, 但它们出现 != 视频下好了。这里以前不看扩展名, 于是
        # "只下了封面"被记成成功, 下一轮又被幽灵清理当成"本地没文件", 变成
        # 每轮重下一次的死循环。
        ext = os.path.splitext(name)[1].lower()
        if ext in VIDEO_EXTS and ext not in PARTIAL_EXTS:
            hits.add(name)
    return hits


def discard_partial_output(folder, before, after, aid, title):
    """删掉这次运行里新出现的、属于这个视频的成片(半成品).

    只在"超时被我们掐掉"时调用: 那时候落盘的东西一定是残缺的, 留着有两个
    坏处 —— 它会被当成"已下载", 而且下次重试时 BBDown 可能觉得文件已经在了
    就不重下。只删这次真正新出现的文件(按「标题_aid」精确匹配),
    多P的情况只删新落下的那几个分P, 不动以前下好的。
    返回删掉的相对路径列表。
    """
    removed = []
    for name in new_video_files(before, after, aid, title):
        full = os.path.join(folder, *name.split("/"))
        try:
            if os.path.isfile(full):
                os.remove(full)
                removed.append(name)
        except OSError:
            continue
    return removed


def reconcile_media(folder, videos, record, index=None):
    """把"本地已有文件但记录里没有"的视频补进记录.

    用来修补「已下载」记录缺失(手工拷进来的文件、以前版本漏记)。
    返回补记的 aid 列表。
    """
    index = index or MediaIndex(folder)
    return _reconcile(folder, videos, record, index)


def _reconcile(folder, videos, record, index):
    from .state import TIME_RECOVERED

    dup = _dup_titles(videos)
    recovered = []
    for v in videos:
        aid = v.get("aid")
        if not aid or aid in record:
            continue
        if index.has(aid, v.get("title", ""), dup):
            record[aid] = {
                "title": v.get("title", ""),
                "bvid": v.get("bvid", ""),
                "time": TIME_RECOVERED,
            }
            recovered.append(aid)
    return recovered


def _dup_titles(videos):
    counts = {}
    for v in videos:
        t = _clean(v.get("title", ""))
        counts[t] = counts.get(t, 0) + 1
    return {t for t, c in counts.items() if c > 1}


def folder_bytes(path):
    """一个文件夹(含子目录)里所有文件的总字节数.

    守护用它来证明"真的在下" —— BBDown 的进度是回车刷新的, 日志里看不到,
    只能看文件夹有没有变大。
    """
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    except OSError:
        pass
    return total
