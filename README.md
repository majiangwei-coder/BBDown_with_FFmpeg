# BBDown 批量补档下载 + 下载守护

在 Windows 上批量下载、**持续补齐** B 站 UP 主全部投稿（以及合集/系列）的小工具。

它把 [BBDown](https://github.com/nilaoda/BBDown) 和 FFmpeg 包成一个双击就能用的东西，
自己负责"盯名单"这件麻烦事：

- **名单化管理**：每个 UP 主 / 合集一个文件夹，配一份 `下载状态.json`。谁下过、
  谁跳过、谁失败，一目了然；再跑一次只补缺口，已经下过的不重复下。
- **下载守护**：撞上限流会自己休息、降速、重开，一轮轮补到齐；撞上充电/付费专属
  自动标记并永久跳过。补完自动退出，不做常驻服务（没有后台进程、没有系统服务，
  随时可以关）。
- **增量同步**：平时检查更新只花 1 次接口请求；每隔几天做一次完整校验兜底。
- **优雅停止**：`stop-everything.bat` 让在下的下完再退，磁盘上不留半成品
  （BBDown 没有断点续传，半成品等于白占地方，这个项目踩过坑）。
- **本地判重**：文件被删了会重新下回来；手动挪过文件夹也认得出来。

> 详细使用说明（目录、每天怎么用、合集怎么下、冻结、常见问题）见
> **[使用说明.md](使用说明.md)**；守护的内部机制（判定规则、互斥锁、测试清单）见
> **[守护模块说明.md](守护模块说明.md)**。

---

## 一、克隆 / 下载

要求：**Windows 10/11**、**Python 3.8 或更新**、**git-lfs**。

仓库里的 `tools\BBDown.exe` 和 `tools\ffmpeg-8.0-full_build\`（约 580 MB）
用 **Git LFS** 存储，所以：

```bat
git lfs install          :: 只需要装一次
git clone <本仓库地址>
```

没装 git-lfs 的话，克隆下来的 exe 只是几百字节的"指针文件"，
BBDown 和 ffmpeg 都跑不起来（页面上的 "Download ZIP" 同理，不带 LFS 内容）。

> 这两个程序都**已经随仓库带好了**，不需要另外去下载。
> 它们是第三方发行版，原样保留 —— 不要删里面的任何文件（见第 5 节）。

## 二、第一次使用（五步）

1. **装依赖**（在项目根目录开一个命令行）：

   ```bat
   pip install -r requirements.txt
   ```

   `requests` 是必需的；`qrcode` 只有扫码登录用得到。

2. **确认 Python 能被找到**：程序自动按
   `BBDOWN_PYTHON` 环境变量 → `tools\python_paths.txt` → 项目里的 `tools\python\`
   → PATH → 常见安装目录 的顺序找。装在默认位置就不用管；
   装在别处就把 `python.exe` 的完整路径写进 `tools\python_paths.txt`（一行一个，# 是注释），
   或者设环境变量 `BBDOWN_PYTHON`。

3. **登录**：双击 `BBDown-身份登录.bat`，用手机 B 站 App 扫码。
   登录凭据只会生成在你自己的
   `tools\BBDown.data` 里（**这个文件不进仓库**，也不会被提交）。

4. **加 UP 主**：双击 `下载管理器.bat` → 按 `N` → 粘贴主页链接
   （`https://space.bilibili.com/数字`）。合集/系列按 `C` 粘贴合集链接。

5. **补缺口**：双击 `启动下载守护.bat`。它会一轮轮把缺的补齐，
   撞限流自己休息重开，补完自动退出。进度随时看 `logs\守护状态.txt`。

## 三、平时只用两个入口

| 想做的事 | 双击 |
| --- | --- |
| 把缺的补上（最常用） | `启动下载守护.bat`（加 `--status` 只看不动；`--forever` 补完继续盯新视频） |
| 挑 UP 主 / 下单个视频 / 下合集 | `下载管理器.bat` |
| 安全停下正在下载的任务 | `stop-everything.bat` |
| 重新扫码登录 | `BBDown-身份登录.bat` |
| 跑测试（离线，不动真实数据） | `运行测试.bat` |

需要无人值守的话，把 `定时更新全部博主(守护).bat` 加进 Windows 计划任务
（它故意不带 `--force`：手动开的守护在跑时，这次就安静让位，不会把你正在跑的杀掉）。

## 四、目录结构

```
BBDown_with_FFmpeg\
  videos\             所有下载好的视频（UP主下载\ / 单视频下载\ / 合集下载\）
  tools\              程序本体：入口脚本、bbdown_kit\（共用代码）、tests\
    BBDown.exe        BBDown 下载器（第三方，随仓库）
    ffmpeg-8.0-full_build\   FFmpeg 发行版（第三方，随仓库）
    BBDown.config     BBDown 自己的配置（ffmpeg 路径、单线程下载）
    下载设置.示例.txt  设置示例，复制成 下载设置.txt 才生效
  logs\               运行日志与状态（守护状态.txt / 守护日志.txt / 守护.log ...）
  使用说明.md           完整使用说明
  守护模块说明.md       守护的技术细节
```

`videos\`、`logs\` 里的东西都是程序运行时自己生成的。

## 五、仓库里**没有**什么（个人数据全部排除）

本仓库的 `.gitignore` 排除了这些东西，**它们在你机器上照常生成、照常使用，
但不会被提交**，别人 clone 也拿不到：

- 登录凭据：`tools\BBDown.data`、`tools\BBDown.data.bak`、`qrcode.png`
- 视频与日志：`videos\`、`logs\`（含冻结名单、永久失败名录、计划任务备份）
- 本机设置：`tools\下载设置.txt`（仓库带的是 `下载设置.示例.txt`）
- 本机路径：`tools\python_paths.txt`
- 运行期文件：`tools\*.lock`、`tools\*-谁在跑.txt`、`tools\*-停止请求.txt`

想改下载参数（并发数、间隔、超时……）：把 `tools\下载设置.示例.txt` 复制成
`tools\下载设置.txt` 再改；不建这个文件时全部用内置默认值。
`tools\ffmpeg-8.0-full_build\` 和 `tools\BBDown.exe` 是第三方发行版，
**原样保留、不要删里面的文件**（少一个文件它就不再是那个发行版了——
这个规矩有测试盯着，见 `tools\tests\test_ownership.py`）。

## 六、第三方程序与许可证

| 组件 | 版本 | 许可证 | 来源 |
| --- | --- | --- | --- |
| BBDown（`tools\BBDown.exe`） | 1.6.3 | MIT | <https://github.com/nilaoda/BBDown> |
| FFmpeg（`tools\ffmpeg-8.0-full_build\`） | 8.0-full_build | GPL v3 | <https://www.gyan.dev/ffmpeg/builds/> |

本仓库自己的代码（`tools\bbdown_kit\`、入口脚本、bat、文档）采用 **MIT**，见
[LICENSE](LICENSE)；第三方组件的声明见 [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)。

## 七、免责声明

本工具只做"下载并保存到你自己的磁盘"这一件事，不破解任何付费/充电内容
（遇到这类视频会直接跳过并记录）。请仅用于**备份你有权访问的内容**，
遵守 B 站的服务条款与著作权法；下载内容的版权归原作者所有。
