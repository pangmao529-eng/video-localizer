# Video Localizer · 批量视频改字

在 Windows 11 x64 的本机把视频里的字幕、角色名牌、界面文字和活动说明改成目标语言。默认韩语，每批 1–20 条，保留原声音轨。视觉推理使用你指定的 Chat Completions 兼容 API。

首次配置时 API 地址、视觉模型 ID 和 API Key 均为空，由你填写。你可以独立选择用于操作插件的 AI 助手，以及用于视频识别和翻译的视觉模型服务。

## 在 AI 助手里开始

把[这个公开仓库](https://github.com/pangmao529-eng/video-localizer)和下面这句话发给能够操作本机文件的 AI 助手。

> 请按这个仓库的 Windows 安装说明，为我安装 Video Localizer 并连接到我当前使用的助手。打开本机模型配置窗口，我自己输入 API Key。装好后等我选择视频。

1. 下载 [Windows x64 安装包](https://github.com/pangmao529-eng/video-localizer/releases/download/v0.1.0-rc.2/YoungTiger-VideoLocalizer-Setup-x64.exe)和[对应的 SHA-256 校验文件](https://github.com/pangmao529-eng/video-localizer/releases/download/v0.1.0-rc.2/YoungTiger-VideoLocalizer-Setup-x64.exe.sha256)。下载目录中运行 `Get-FileHash .\YoungTiger-VideoLocalizer-Setup-x64.exe -Algorithm SHA256`，将结果与校验文件里的 64 位十六进制值比对。[v0.1.0-rc.2 预发行说明](https://github.com/pangmao529-eng/video-localizer/releases/tag/v0.1.0-rc.2) · [本版源码](https://github.com/pangmao529-eng/video-localizer/tree/v0.1.0-rc.2)
2. 核对一致后运行安装包。它安装在当前 Windows 用户下，自带 Python、FFmpeg、FFprobe、图像处理库和韩文字体。此预发行包未作发布者代码签名。
3. 在开始菜单打开 **Video Localizer 模型配置**，或在 PowerShell 执行下面的命令。填入服务商给出的 API 地址、可看图的模型 ID、API Key。密钥通过遮挡输入进入 Windows 凭据管理器。支持带 `/v1` 的服务根地址或完整 `/chat/completions` 端点，本机模型可使用回环 HTTP 地址。模型须支持图文消息和 JSON 格式回答。

```powershell
$vl = Join-Path $env:LOCALAPPDATA 'YoungTiger\VideoLocalizer\video-localizer.exe'
& $vl setup --gui
& $vl doctor
```

### 连接一个助手

在同一个 PowerShell 窗口运行与你使用的助手对应的一条命令。

| 助手 | 命令 |
| --- | --- |
| Codex（已安装 Codex CLI） | `& $vl connect --host codex` |
| Claude Code（已安装 Claude CLI） | `& $vl connect --host claude` |
| Cursor | `& $vl connect --host cursor` |
| VS Code 中的 GitHub Copilot | `& $vl connect --host copilot` |
| 其他支持本地 MCP 的助手 | `& $vl connect --host generic`，将返回的启动参数填入该助手的 MCP 设置 |

注册后按客户端提示重新载入工具。若客户端由组织管理，使用其允许的 MCP 或本地终端入口。在助手里说“打开 Video Localizer 的模型配置窗口”，助手可以调用 `open_model_configuration`。随后在本机窗口填写配置并保存，调用 `check_setup` 查看本机依赖和密钥配置状态。没有 MCP 的本机助手可以直接运行下面的命令行。

## 批量改字

> 把 D:\广告素材\本批 里的 MP4 全部改成韩语，保留原声和原画幅，输出到 D:\广告素材\韩语成片。有看不清或被遮住的字就列出时间点，继续做其他视频。

命令行等价操作如下。输出目录会生成一份独立批次，重跑会保留上一版。选择文件夹时只取该目录中直接包含的 MP4，超过 20 条时可以指定所需的文件。

```powershell
& $vl convert 'D:\广告素材\本批' --target 韩语 --output 'D:\广告素材\韩语成片'
```

**可选资料**可以在助手指令里直接写文件路径，或用命令行参数提供。

| 参数 | 可传入的资料 |
| --- | --- |
| `--translations` | 定稿，UTF-8 CSV、TSV 或 JSON。CSV/TSV 首行用 `source,target` 或相应的制表符分隔。 |
| `--glossary` | 同格式的项目术语表。表格只有 Excel 格式时，可另存为 UTF-8 CSV。 |
| `--references` | 角色参考图目录（文件名为角色名），或名字到同目录内图片相对路径的 JSON 对象。支持 JPEG、PNG、WebP。 |
| `--source-language` | 默认自动识别。混合语言或小字较多时可指定源语言。 |
| `--profile 2k120` | 长边 2560，保持比例，以重复源帧输出 120 fps。默认 `source` 保留原片规格。 |

原片适用范围为 MP4、每条最多 5 分钟和 512 MiB、长边不超过 1920、短边不超过 1080、最多 120 fps、一条视频流及最多一条音轨。源规格改字使用恒定帧率；可变帧率素材可选择 `2k120`。首次处理时留出临时磁盘空间。

**改字规则。** 优先采用匹配的定稿，继续使用术语表中的名称。名牌跟随画面中能够辨认的真实角色，在立绘实际切换时换字。原片与成片均在本机，选中的画面、相关文字和按需使用的角色参考图会送到你配置的模型端点。遮挡、定位不稳或无法辨认的区间会保留原画。模型读图能力会影响实际效果和服务商计费。

**审核。** 打开输出批次里的 `审核.html` 观看 MP4、查看待复核时间码。每条成片旁边的 `.review.json` 保存原文、疑难区间、自动纠正的名牌及用量记录。 `batch.json` 列出成功、失败和尚未处理的文件。若整条没有可确认的改字，结果会标记相应状态。鉴权、计费或接口不可用会停止后续请求。

## 从源码构建

在 Windows x64 上使用 Python 3.11 或更高版本运行下列命令。构建脚本从锁定来源获取依赖并校验摘要，再生成离线安装包。

```powershell
python packaging/build_windows.py --output-dir dist --cache-dir build/dependency-cache
```

自有代码使用条款见 `LICENSE.txt`，第三方组件按随包许可使用。仓库示例与工作流独立于任何用户的原片、定稿及密钥。
