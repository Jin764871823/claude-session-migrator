# Claude 会话迁移

**中文** | [English](README.en.md)

Claude 桌面端换了登录账号（或在 claude.ai 登录和 API key 模式之间切换）后，侧栏里的旧对话全不见了。其实对话还在电脑上，只是新账号的会话列表是空的。这个小工具把旧账号的会话列表搬到新账号，桌面端重新打开后旧对话回到侧栏，点开就能接着聊。

![迁移页](docs/images/main.png)

## 核心功能

- **一页选好**：左边勾选旧账号，右边是新账号（默认最近登录的那个）。每个账号显示对话数和最近一条对话的标题，一眼认出是哪个号；下方实时显示要搬多少个对话，可逐条查看明细。
- **一键迁移**：只确认一次，自动完成 关闭桌面端 → 备份并校验 → 写入 → 重新打开桌面端。只关桌面端本体，终端里的 Claude Code 不受影响。
- **随时撤销**：每次迁移前自动备份；「记录」页一键撤销，只移走那次写入、之后没再动过的对话。
- **不伤数据**：只复制侧栏的会话记录（`local_*.json`），对话原文（`.jsonl`）从不读写；新账号里已有的不会被旧版本覆盖，新账号里删掉的不会被搬回来。
- **两种模式都支持**：同时扫描 claude.ai 登录版和 API key 版（`Claude-3p`）的全部账号。

| 确认一次，自动跑完 | 按次撤销 |
|---|---|
| ![确认框](docs/images/confirm.png) | ![记录页](docs/images/history.png) |

## 使用

需要 Python 3.8+（自带 Tkinter，不用装别的库）。双击 `run.bat`，或运行 `python gui.py`。

想要不依赖 Python、带图标的 exe（Windows）：装好 PyInstaller 后运行 `python build_exe.py --out <新的空目录>`，得到单个文件 `dist\Claude会话迁移.exe`（自包含，复制这一个文件就能用）。只打包已提交的源码。

1. 在 Claude 桌面端登录**新**账号，打开一次 **Code 标签页**（这时才会生成新账号的目录）。
2. 打开本工具，确认左右两边的账号，点 **开始迁移**。
3. 桌面端重新打开后，进 Code 标签页，旧对话就在侧栏里。

搬错了：点「撤销这次迁移」，或到「记录」页撤销。没看到旧对话：点底部的「重启」，并确认登录的就是迁入的那个账号。

## 原理

桌面端把侧栏列表按账号分目录存放，对话原文则与账号无关，两边靠 `local_*.json` 里的 `cliSessionId` 对应：

| 内容 | 位置（Windows） | 按账号区分 |
|---|---|---|
| 侧栏列表 | `%APPDATA%\Claude\claude-code-sessions\<账号>\<组织>\local_*.json`<br>API 版在 `%LOCALAPPDATA%\Claude-3p\claude-code-sessions\` | 是 |
| 对话原文 | `~\.claude\projects\<目录>\<session-id>.jsonl` | 否 |

换号后新账号的目录是空的，把旧账号的记录拷过去就行。

**不迁移的内容**：定时任务、backlog、Cowork 数据、侧栏分组和排序（会话自带的收藏标记会随记录过去）。新账号缺少原来的 MCP 服务器或连接器时，会话照样能打开，只是用不了那些工具。

## 安全要点

- 写入前整目录备份新账号的会话目录，并核对文件数和大小，对不上就停下、什么都不写。
- 写入了哪些文件（含 sha256）记在备份里的清单中；撤销只动内容没变过的文件，移到旁边的文件夹，不删除。
- 同一对话在旧账号里更新过时，默认覆盖新账号里的旧版本（旧版本留在备份里，可在「高级选项」关闭）。
- 关闭桌面端前按 PID 和程序路径复核，只结束桌面端本体；读不到路径的进程不碰，并在确认框里提醒。关不掉就取消，什么都不写。

## 命令行

给脚本或 AI 用，和界面共用同一套逻辑。默认只预览，加 `--apply` 才写入：

```text
python core.py --list                          列出账号
python core.py                                 预览：所有旧账号 → 最近登录的账号
python core.py --source 2 --target 1 --apply   按 --list 的序号执行
python core.py --undo --backup 1 --apply       撤销第 1 个备份
```

更多选项见 `python core.py --help`。想先拿副本试用：`python gui.py --base <claude-code-sessions 副本>`（此时不检测、也不关闭桌面端）。

Windows 已实测；macOS / Linux 的目录按 Electron 默认推断，自动关闭和重开桌面端只在 Windows 上实现。

## 文件

`gui.py` 图形界面（只用标准库）· `ui_theme.py` 界面主题（配色、按钮、卡片等部件，和作者其他工具共用的副本）· `core.py` 逻辑层与命令行 · `app_icon.py` 程序图标（代码画的，不放图片文件）· `build_exe.py` 打包成 exe · `selftest.py` 自测（在合成目录上跑，不碰真实数据）· `run.bat` 启动器 · `scripts/preview.py` 用演示数据重拍 README 截图

另附一个命令行小工具 `rename_projects.py`：项目目录改名（比如中文名改英文名）时，把 Claude Code / 桌面端的会话、记忆、侧栏和设置一起迁到新路径，带预演、备份和回滚；配置样例 `rename_config.example.json`，自测 `selftest_rename.py`，用法见文件开头的说明。

## 致谢

迁移原理参考 [xing0325/claude-account-switch-migration](https://github.com/xing0325/claude-account-switch-migration)；识别最近登录账号、源更新时同步、备份校验、日志、目录结构校验和默认只预览的命令行，借鉴了 [mofchris/transfer-claude-sessions](https://github.com/mofchris/transfer-claude-sessions)（MIT）。本项目是独立的 Python + Tkinter 实现。
