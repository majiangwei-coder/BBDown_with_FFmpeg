# -*- coding: utf-8 -*-
"""下载设置.txt 的解析测试.

设置文件是给人手改的, 所以这里测的都是"手改会怎么改坏":
  · 记事本"另存为 Unicode"存成 UTF-16;
  · 路径顺手加了引号;
  · 数字写成了 inf / nan;
  · 某一行写坏了。
要求是"坏的那一项退回默认值, 其它设置照常生效", 而不是整份设置作废。
"""

import os
import tempfile
import unittest

from support import paths  # noqa: F401  (把包挂到 sys.path 上)
from bbdown_kit import config


class SettingsFileTest(unittest.TestCase):

    def load(self, raw):
        """把字节写进临时文件, 走真实的 load_settings()."""
        fd, path = tempfile.mkstemp(prefix="bbdown_cfg_", suffix=".txt")
        self.addCleanup(os.remove, path)
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        return config.load_settings(path)

    # ---- 编码 ----

    def test_utf16_file_is_read_not_silently_ignored(self):
        """记事本"Unicode"存出来的 UTF-16 必须能读出来.

        UTF-16LE 的内容是 ASCII 字符加 \\x00, 对 utf-8 来说是**合法字节**,
        旧写法会"成功"解出一串带 NUL 的乱字符 -> 每一行都认不出来 ->
        整份设置静默退回默认值(用户改了设置却毫无反应)。
        """
        text = "parallel=5\ninterval=2.5\n视频目录=D:\\videos\n"
        for bom in ("utf-16", "utf-16-le", "utf-16-be"):
            raw = text.encode(bom)
            if bom == "utf-16-le":
                raw = b"\xff\xfe" + raw
            elif bom == "utf-16-be":
                raw = b"\xfe\xff" + raw
            st = self.load(raw)
            self.assertEqual(st["parallel"], 5, bom)
            self.assertEqual(st["interval"], 2.5, bom)
            self.assertEqual(st["data_root"], "D:\\videos", bom)

    def test_utf8_with_bom_still_works(self):
        st = self.load("parallel=3\n".encode("utf-8-sig"))
        self.assertEqual(st["parallel"], 3)

    def test_gbk_file_still_works(self):
        st = self.load("并行下载数=4\n".encode("gbk"))
        self.assertEqual(st["parallel"], 4)

    # ---- 值 ----

    def test_quoted_data_root_is_unquoted(self):
        """data_root="D:\\videos" 里的引号会变成路径非法字符."""
        for line in ('data_root="D:\\videos"',
                     "data_root='D:\\videos'",
                     '视频目录= "D:\\videos" '):
            st = self.load(line.encode("utf-8"))
            self.assertEqual(st["data_root"], "D:\\videos", line)

    def test_unquoted_data_root_is_untouched(self):
        st = self.load(b'data_root=D:\\a b\\videos\n')
        self.assertEqual(st["data_root"], "D:\\a b\\videos")

    def test_non_finite_interval_falls_back_to_default(self):
        """inf/nan 会让"等一会儿"变成永不等待/立刻重试."""
        for value in ("inf", "-inf", "nan", "Infinity"):
            st = self.load(("interval=%s\n" % value).encode("ascii"))
            self.assertEqual(st["interval"], config.DEFAULTS["interval"], value)

    def test_normal_interval_still_works(self):
        for value, want in (("0", 0.0), ("2.5", 2.5), ("30", 30.0)):
            st = self.load(("interval=%s\n" % value).encode("ascii"))
            self.assertEqual(st["interval"], want, value)

    # ---- 坏行 ----

    def test_one_broken_line_does_not_kill_the_rest(self):
        st = self.load(b"parallel=abc\ninterval=4\nfail_threshold=zz\n"
                       b"full_days=3\n")
        self.assertEqual(st["parallel"], config.DEFAULTS["parallel"])
        self.assertEqual(st["fail_threshold"],
                         config.DEFAULTS["fail_threshold"])
        self.assertEqual(st["interval"], 4.0)
        self.assertEqual(st["full_days"], 3)

    def test_missing_file_gives_defaults(self):
        st = config.load_settings(os.path.join(tempfile.gettempdir(),
                                               "bbdown_no_such_settings.txt"))
        self.assertEqual(st, config.DEFAULTS)


if __name__ == "__main__":
    unittest.main()
