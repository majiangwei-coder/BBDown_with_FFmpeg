# -*- coding: utf-8 -*-
"""归属规矩: 工具部分不许少文件, 我们的代码删了要有依据.

这套测试是被一次真实事故逼出来的:
    收尾清理时看到 ffmpeg 发行版里的 ffplay.exe(187 MB), 理由是
    "BBDown.exe 里 ffplay 出现 0 次, 代码零引用, 用不到", 于是删了。
    **这个理由本身没错, 但前提错了** —— ffplay 不是我们的冗余代码,
    它是 FFmpeg 发行版自带的一个工具。少一个文件, 这个包就不再是那个
    发行版了; doc\\ 和 presets\\ 同理。

所以规矩写死在这里:
    工具部分 —— 原样保留, 只允许整包换成新版本。
    我们的代码 —— 可删, 但删之前必须说得出 (a)功能不要了 (b)已被取代
                   (c)结构上不再需要 三者之一。
"""

import os
import unittest

from support import REAL_ROOT, REAL_TOOLS, RealProjectTest

# 三方工具里**必须有**的东西(发行版的完整性)。
# 注意: 这里只钉"少了会出事"的关键项, 不逐个列 44 个文件 ——
# 那份完整清单在 tools\工具清单.txt, 由 清点工具与代码.py 负责核对。
FFMPEG_BUILD = os.path.join(REAL_TOOLS, "ffmpeg-8.0-full_build")
REQUIRED_TOOLS = [
    # 程序真的会调用它们(少一个就跑不起来)
    os.path.join(REAL_TOOLS, "BBDown.exe"),
    os.path.join(FFMPEG_BUILD, "bin", "ffmpeg.exe"),
    os.path.join(FFMPEG_BUILD, "bin", "ffprobe.exe"),
    # 发行版自带、程序不调用, 但**同样不许删**
    # (ffplay 就是这么被误删的: "代码没引用"不等于"它能删")
    os.path.join(FFMPEG_BUILD, "bin", "ffplay.exe"),
    os.path.join(FFMPEG_BUILD, "LICENSE"),
    os.path.join(FFMPEG_BUILD, "README.txt"),
    # 发行版的完整目录结构
    os.path.join(FFMPEG_BUILD, "doc"),
    os.path.join(FFMPEG_BUILD, "presets"),
    # 配置(我们发的, 要在)
    os.path.join(REAL_TOOLS, "BBDown.config"),
]

# 登录 cookie 单独管: 它由扫码登录生成, 全新 clone 里还没有(也故意不进仓库),
# 所以"缺了"不算破坏规矩 —— 跳过并提示先登录, 而不是判失败。
LOGIN_FILE = os.path.join(REAL_TOOLS, "BBDown.data")


class TestThirdPartyToolsAreComplete(RealProjectTest):
    """工具部分: 一个都不许少."""

    def test_required_tool_files_exist(self):
        for path in REQUIRED_TOOLS:
            self.assertTrue(
                os.path.exists(path),
                "工具文件不见了: %s\n"
                "工具是第三方发行版的一部分, 不能因为'代码没调用'就删 —— "
                "真要改版本请整包替换, 并在 tools\\工具清单.txt 里更新那一行。"
                % os.path.relpath(path, REAL_ROOT))

    def test_login_cookie_exists_or_you_just_need_to_log_in(self):
        """BBDown.data 是扫码登录后才生成的; 全新 clone 还没登录时跳过."""
        if not os.path.exists(LOGIN_FILE):
            self.skipTest(
                "还没登录过, 所以 tools\\BBDown.data 不存在 —— 这不是故障。"
                "双击 BBDown-身份登录.bat 扫码登录, 它就会生成, 再跑测试即通过。")

    def test_ffmpeg_build_file_count(self):
        """ffmpeg 发行版是 44 个文件; 少了就说明被删过.

        数字写死是有意的: 它不是"我们想要 44 个", 而是"这个发行版就是 44 个"。
        换了新版本(比如 9.0)才需要改这个数字, 那时候也请一起改
        tools\\工具清单.txt。
        """
        n = sum(len(files) for _b, _dirs, files in os.walk(FFMPEG_BUILD))
        self.assertEqual(n, 44,
                         "ffmpeg 发行版应该正好 44 个文件, 现在 %d 个 —— "
                         "少了就是被删过, 多了就是有东西掉进去了" % n)

    def test_ffmpeg_doc_and_presets_are_not_empty(self):
        """doc/ 和 presets/ 要真的还有东西(不是空目录)."""
        self.assertGreaterEqual(len(os.listdir(os.path.join(FFMPEG_BUILD, "doc"))),
                                30, "ffmpeg 的离线文档被删过")
        self.assertGreaterEqual(
            len(os.listdir(os.path.join(FFMPEG_BUILD, "presets"))), 5,
            "ffmpeg 的编码预设被删过")

    def test_manifest_lists_every_tool(self):
        """工具清单必须列到这些关键项, 否则清点脚本会漏判."""
        manifest = os.path.join(REAL_TOOLS, "工具清单.txt")
        self.assertTrue(os.path.exists(manifest), "工具清单.txt 不见了")
        with open(manifest, encoding="utf-8-sig") as f:
            text = f.read()
        for needle in ("BBDown.exe", "ffmpeg-8.0-full_build", "BBDown.data",
                       "BBDown.config"):
            self.assertIn(needle, text, "工具清单里没列 %s" % needle)


class TestCleanupRuleIsDocumented(unittest.TestCase):
    """规矩要写在能被看见的地方, 不能只留在某次对话里."""

    def test_manifest_explains_the_rule(self):
        with open(os.path.join(REAL_TOOLS, "工具清单.txt"),
                  encoding="utf-8-sig") as f:
            text = f.read()
        self.assertIn("原样保留", text)
        self.assertIn("我们的代码", text)
        for hint in ("功能不要了", "取代", "不再需要"):
            self.assertIn(hint, text, "清单里没说清哪种情况才能删我们的代码")

    def test_inventory_tool_is_read_only(self):
        """清点脚本只读: 不许出现删除/写入动作."""
        path = os.path.join(REAL_TOOLS, "清点工具与代码.py")
        self.assertTrue(os.path.exists(path), "清点工具与代码.py 不见了")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        for forbidden in ("rmtree", "os.remove", "os.unlink", "shutil.move"):
            self.assertNotIn(forbidden, src,
                             "清点脚本是只读的, 不该出现 %s" % forbidden)
        self.assertNotIn('open(path, "w"', src)
        self.assertNotIn("_save(", src.split("def read_manifest")[0])


if __name__ == "__main__":
    unittest.main()
