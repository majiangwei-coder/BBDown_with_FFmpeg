# 第三方组件声明

本仓库里**我们自己的代码**（`tools\bbdown_kit\`、各入口脚本、`tools\tests\`、bat、文档）
采用 MIT 许可证，见根目录 `LICENSE`。

但仓库里还**携带了两个别人的程序**。它们各自是独立的作品，按各自的许可证分发，
本项目的 MIT 许可证**不覆盖**它们；本仓库没有对它们做任何修改。

---

## BBDown

| | |
| --- | --- |
| 位置 | `tools\BBDown.exe` |
| 版本 | 1.6.3（文件版本信息：`1.6.3+45622f79cd766e0fc6f5cbd49fcf4960340f35c3`） |
| 上游 | <https://github.com/nilaoda/BBDown> |
| 许可证 | MIT |

```
MIT License

Copyright (c) BBDown contributors (nilaoda)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

> 上面是 MIT 许可证的标准文本；版权行以 <https://github.com/nilaoda/BBDown> 仓库里的
> `LICENSE` 文件为准。

## FFmpeg

| | |
| --- | --- |
| 位置 | `tools\ffmpeg-8.0-full_build\` |
| 版本 | `8.0-full_build-www.gyan.dev` |
| 来源 | <https://www.gyan.dev/ffmpeg/builds/> |
| 许可证 | GPL v3 |
| 许可证全文 | 随发行版自带：`tools\ffmpeg-8.0-full_build\LICENSE` |
| 构建信息 / 对应源码提交 | `tools\ffmpeg-8.0-full_build\README.txt`（写明源码为 <https://github.com/FFmpeg/FFmpeg/commit/140fd653ae>） |

这是 gyan.dev 提供的官方静态构建，完整、未修改。该构建按 **GPL v3** 分发，
所以这一份 `ffmpeg.exe` / `ffprobe.exe` / `ffplay.exe` 同样按 GPL v3 分发；
对应源码可以从它 README 里写明的提交获取，或从 <https://ffmpeg.org/download.html> 获取。

`bin\ffplay.exe`、`doc\`、`presets\` 等同样是这个发行版的一部分，随发行版一并分发。

---

## 再分发时请注意

如果你把这个仓库（或仓库里的第三方程序）再分发出去，请一并保留它们各自的
许可证与声明：BBDown 的 MIT 声明、FFmpeg 的 GPL v3 许可证全文
（`tools\ffmpeg-8.0-full_build\LICENSE`）。删掉这些文件就不再是原来的发行版了。
