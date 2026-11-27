# Video Localizer · 云端视频改字

> 2026-11-27 进度快照：最新要求是由云端模型根据提示词完成识别、擦字、韩文生成与跟踪。此前的服务器手工排版实验已停止；下述 rc21 MediaKit 渲染分支保留作历史代码，不代表已满足这一新要求。后20秒测试仍有游戏界面与技能说明残字，完整30秒成片尚未通过验收。详见[当前进度与交接](docs/progress/2026-11-27-cloud-only-handoff.md)和[云端提示词](docs/progress/2026-11-27/cloud-only-full30-prompt.txt)。

`0.2.0rc21` 是适用于 Windows 11 x64 和 Codex MCP 的视频批量改字插件。新增 MediaKit 精细字幕流程：云端 OCR 与精细擦除、已定稿译文精确绑定、服务器局部补片和韩文字体渲染，保留原声。Seedance 2.0 流程仍支持由服务器分段、云端编辑、服务器拼接。桌面负责上传、保存断点、调用 API 和下载成片后的技术检查。

MediaKit 配置、云端任务和断点与 Seedance 分开，旧版安装不会自动获得新入口。旧混元控制器、模型客户端、帧编辑执行器及对应测试移至 `legacy/hunyuan/`，不进入安装包。迁移时把不含密钥的旧混元档案保存在本机 `legacy-profiles.json`；旧凭据与此前安装版本保留供回退。

MediaKit OCR 当前仅支持简体中文和英语，韩文成片的 OCR 输出可能出现中文乱码。因此该步骤保留观察记录，不能自动确认残字或韩文准确率；成片需要视觉复核。参考：[官方语种限制](https://docs.volcengine.com/docs/intelligent-processing/video-ocr-guide?lang=zh)。

## 当前验证边界

2026-11-26 的服务器上传实测已确认：20.1 秒原片分成两段，两段均成功上传自有素材服务器并取得 Seedance 任务 ID。平台随后对两段返回 `OutputVideoSensitiveContentDetected.PolicyViolation`，即生成内容可能涉及版权限制。这不是上传失败，也不是成功成片；原任务和断点保留，不自动重新提交。

各版本的发布记录见随安装包提供的验证报告。rc18 的 16 秒合成测试已跑通上传、两段生成、下载、拼接与原声保留，但整片多出 2 帧，严格时长检查未通过。rc19 引入服务器分切与拼接；历史版本报告不能代替新版真实验证。上传、鉴权、任务提交、下载、技术检查与人工视觉验收分别记录。

rc20 修复请求等待被底层 30 秒上限提前截断的问题：连接阶段最多 30 秒，创建任务使用独立 180 秒总预算，普通查询默认 60 秒，发送和读取按剩余总预算收紧。生成请求仍不自动重试。服务器批次在提交前保存每次尝试和 `X-Client-Request-Id`，失败时保存连接/发送/响应阶段、异常类型、已完整发送字节数和平台请求 ID，不保存密钥、签名地址或请求正文。追踪 ID 用于关联记录，不是幂等键；历史未知提交不会因升级被自动重发。

## Seedance 工作流程

1. 读取视频路径、目标语言及可选的定稿表、术语表、参考图；宿主原样转交要求，不代做翻译或判断画面。
2. 检查源片元数据和 SHA-256，用独立上传凭据将整条原片按 256 KiB 分块传到自有 HTTPS 服务器，按服务端偏移续传；完整对象按 SHA-256 复用。
3. 服务器创建持久分切任务，按原帧边界每 15 秒切分，最多 20 段。最后不足 4 秒时从前段借帧，保证每段 4–15 秒且不丢帧、不重复帧；例如 43.4 秒切成 15、15、13.4 秒。该过程不识别镜头、不改写画面内容。
4. 服务器签发短期只读 URL，插件在内存中交给 Seedance `reference_video`；签名 URL 不写入批次或日志，Ark 密钥不发送给素材服务器。
5. POST 创建云端编辑任务，持久保存任务 ID，再通过 GET 查询同一任务。提交响应不确定时暂停，避免重复计费。
6. 插件将 Seedance 成功任务的临时结果 URL 交给素材服务器；服务器自行下载各段，校验后以视频流复制方式拼接并复用原片音轨。地址只用于当前下载，不写入任务记录或日志。失败段不会被跳过后拼成缺失内容的整片。
7. 客户端仅下载已拼接的整片，不下载分段来本地拼接。服务器与本机分别校验解码、源片哈希、尺寸、时长、帧率、音轨和同步，不通过裁帧、补帧或重定时隐藏云端输出差异。候选进入 `needs_review`；只有人工检查整片并明确提交 `keep`，且技术检查通过，才记为完成。

提示词要求完整去除残字和描边，保持译文运动、透视和缩放，处理前景遮挡、切镜边界、短暂文字与横竖排。云端任务成功不保证这些要求全部满足。本版没有独立自动视觉验收。

## MediaKit 精细字幕分支（源码新增）

这一分支用于已有中文对白字幕和用户定稿韩语译文的素材。MediaKit API 负责 OCR 时间、位置识别及精细字幕擦除；插件只做严格文本绑定和断点编排，自有素材服务器负责把韩文绘制回画面并复用原声。本机不执行擦字或绘字，不要求 Seedance 生成整幅画面，也不自动翻译游戏界面中的任意文字。

译文文件必须是 UTF-8 JSON 数组，每项包含 `source` 和 `target`，例如：

```json
[{"source":"师姐，你的战力怎么提升这么快？","target":"사저, 전투력 왜 이리 빨리 올라?"}]
```

只有 OCR 完整匹配到的原文才绑定译文；去除标点和空白后仍有差异、缺字或时间冲突时暂停检查，不猜词、不自行改译，也不插入视频中尚未出现的后续台词。连续字幕可拼回同一条定稿；按两种语言已有句号、问号和感叹号分屏时，重新合并后的韩文必须逐字符等于用户原稿。`Others` 水印继续忽略；仅当它唯一命中定稿的完整分句，且前后已识别字幕的时间、位置吻合时，才记录 `exact_reference_clause` 标签纠正。

安装包含新入口的版本后，在本机交互终端运行：

```powershell
$vl = Join-Path $env:LOCALAPPDATA 'YoungTiger\VideoLocalizer\video-localizer.exe'
& $vl setup-mediakit --model-version v5.6
& $vl mediakit-doctor
& $vl mediakit-convert 'D:\素材\前10秒.mp4' --translations 'D:\素材\定稿.json' --output 'D:\素材\韩语测试' --max-polls 60
```

`setup-mediakit` 通过隐藏输入保存独立 MediaKit 凭据；不提供把密钥放入命令行的参数，也不改变 Ark 凭据。`mediakit-doctor` / MCP `check_mediakit_setup` 仅检查已保存配置，不访问网络，不代表计费权限、素材服务器新渲染能力或成片质量已验证。素材服务器仍需要独立配置和上传凭据，以及支持 `render_subtitles` 的服务版本。

MCP 的 `convert_subtitles` 接受单条 MP4 绝对路径 `input_path`、定稿 JSON 绝对路径 `translations`、输出绝对目录 `output_dir`，`max_polls` 为 1–200，默认 60。该操作会调用付费 OCR 和字幕擦除。用 `batch_progress` 查询进度，`cancel_batch` 停止本地等待；已提交的云端任务可能继续计费。

可增加 `--stop-after ocr` 或 `--stop-after erase`，在对应阶段完成后保存 `paused_checkpoint`。续跑须使用独立入口，已有任务 ID 和幂等标记继续复用：

```powershell
& $vl mediakit-resume --batch-dir 'D:\素材\韩语测试\mediakit-实际批次名' --max-polls 60
```

MCP 对应 `resume_subtitles`。继续完整流程时省略 `stop_after`；保留它会再次停在指定阶段。`paused_checkpoint`、`paused_budget` 和 `needs_review` 均不是成片验收通过，CLI 会返回非零退出码。当前 MediaKit 候选仍需检查擦除区域、韩文字形与换行、对白时序、原画面和原声保留；Seedance 的 `submit_review` 不用于 MediaKit 断点。新分支的真实验证结果应另行记录，不以历史 Seedance 报告代替。

## 安装、配置与连接

校验 `.exe.sha256` 后运行 `YoungTiger-VideoLocalizer-Setup-x64.exe`。安装在当前用户的 `%LOCALAPPDATA%\YoungTiger\VideoLocalizer`，不需要管理员权限。

配置窗口分别保存：

- Ark API 地址、模型 ID 或 `ep-...` 接入点、对应 Seedance 系列、Ark API Key。
- 自有素材服务器地址（公共 HTTPS、路径为 `/codex-media/v1`）与独立上传凭据。

密钥保存在 Windows 凭据管理器，配置 JSON 只保存非秘密字段。新填入的 `ep-...` 接入点必须重新选择实际开通的系列；更换接入点不会沿用旧系列推断。上传地址不能使用 Ark 主机或同一凭据目标，留空密钥只复用对应地址已保存的凭据。

默认模型为 `doubao-seedance-2-0-260128`，Ark 地址为 `https://ark.cn-beijing.volces.com/api/v3`。支持已明确配置的 Seedance 2.5；升级保留当前工作模型，不擅自换型号。密钥可用不表示模型已开通。

```powershell
$vl = Join-Path $env:LOCALAPPDATA 'YoungTiger\VideoLocalizer\video-localizer.exe'
& $vl setup --gui
& $vl doctor
& $vl connect --host codex
```

已有 Codex MCP 配置时，升级后新建连接才能加载新版本。`check_setup` 返回实际运行进程的版本、PID、revision、提示词版本及素材上传配置是否齐全；`active.txt` 只决定未来启动的版本。也可用 `connect --host generic` 查看标准 stdio 配置。

`doctor` 不访问网络；`video_workflow_ready` 要求本机依赖、Seedance 凭据和素材上传凭据均齐全，不表示服务器处理能力已在线验证。新 2.0 批次需要素材服务 1.2.0 的任务接口；服务器缺少该能力时不会改为本地分切。只读 API 鉴权测试不上传素材、不生成视频，也不证明模型权限或成片质量：

```powershell
& $vl test-model --timeout 60
```

自有素材服务器是单独部署的组件，见 `server/README.md`。桌面安装包不包含服务器凭据或用户专用部署脚本。

## 在 Codex 中使用

例如：

> 用 Video Localizer 把 D:\素材\本批 的视频改成韩语，保留原声和画幅，输出到 D:\素材\韩语成片。所有识别、翻译和视频修改由 Seedance 完成，制作后保留待审核候选。

也可通过命令行：

```powershell
& $vl convert 'D:\素材\本批' --output 'D:\素材\韩语成片' --target '韩语' --profile source
```

新 2.0 服务器流程的输入限制为 4–300 秒、24–60 fps 恒定帧率、原片不超过 200 MiB、支持的 MP4 编码和画幅。2.0 支持直接模型 ID 或绑定 `seedance-2.0` 的接入点。2.0 的 `duration=-1` 由模型决定输出时长，云端帧率也可能与源片不同；技术检查会明确报告。当前不承诺 2K/120 fps 输出。Seedance 2.5 与旧批次保留此前工作方式和 4–30 秒限制。

可附 `--translations`、`--glossary`、`--references` 及 `--request`。资料冲突原样交给模型，不静默合并定稿或截断超限内容。没有定稿资料时，由 Seedance 翻译，结果仍需人工验收。

## Seedance 断点与审核

批次目录保存 `batch.json`（schema 3）、审核页面及每条视频的工作目录。新版标记 `processing_backend: server-v1`，保存服务器分切、各段 Seedance、服务器结果导入及拼接任务的 ID 和哈希；本机只保存整片候选。旧版的本机分段清单和子任务仍可由原路径续跑，不静默迁移。源片、提供方、模型、系列、API 地址、素材后端及资料指纹绑定断点，变化后不能混用已有任务。

| 状态 | 含义 |
| --- | --- |
| `paused_budget` | 本次状态查询次数用完，可续跑原任务；不是模型 token 额度 |
| `paused_error` | 上传、网络、配置或提交不确定，需要按记录处理 |
| `failed` | 平台任务或本地校验失败，保留原因和已有任务 |
| `cancelled` | 本机已停止；服务器工作及已提交的云端任务可能继续，云端任务可能计费 |
| `needs_review` | 候选已保留，等待人工意见；报告可能仍有技术问题 |
| `completed` | 当前候选技术检查通过且已取得人工 `keep` |

`max_turns` 是每条视频本次运行的状态查询上限，1–200；不控制 Seedance 输出 token。续跑有任务 ID 的记录只查询同一个任务，平台版权拒绝不会触发自动重新生成。已有完整候选可重新校验，不重复下载或生成。

```powershell
& $vl resume --batch-dir 'D:\素材\韩语成片\batch-实际批次名' --max-turns 80
```

人工审核仅接受 `keep` 或 `reject`，可附 `note`，例如 UTF-8 `decision.json`：

```json
{"action":"keep","note":"已逐段检查整片文字、遮挡、切镜与音画同步。"}
```

```powershell
& $vl review --batch-dir 'D:\素材\韩语成片\batch-实际批次名' --video-index 1 --issue-id seedance-full-video-review --decision '@D:\素材\decision.json'
```

决定必须来自人工，宿主不得代选。技术失败会阻止 `keep`。拒绝当前候选后，需要重新编辑时创建带明确修改要求的新批次，不自动重复收费。旧混元 schema 2 断点只能由相应旧版本处理。

## 升级与源码验证

安装器将每个版本放入独立 `versions` 目录，写入完成后切换 `active.txt`，保留旧版本和用户凭据。回退可运行已保留的旧安装包，再重新连接 MCP；不要删除仍被运行任务使用的版本目录。

本地回归使用合成视频和模拟 API，不需要真实密钥：

```powershell
$pluginRoot = Join-Path $env:LOCALAPPDATA 'YoungTiger\VideoLocalizer'
$revision = (Get-Content -LiteralPath (Join-Path $pluginRoot 'active.txt') -Raw).Trim()
$runtimeRoot = Join-Path $pluginRoot ('versions\' + $revision)
$env:VIDEO_LOCALIZER_TEST_FFMPEG = Join-Path $runtimeRoot 'bin\ffmpeg.exe'
$env:VIDEO_LOCALIZER_TEST_FFPROBE = Join-Path $runtimeRoot 'bin\ffprobe.exe'
$env:VIDEO_LOCALIZER_TEST_FONT = Join-Path $runtimeRoot 'fonts\NotoSansCJKkr-Regular.otf'
& (Join-Path $runtimeRoot 'runtime\python.exe') -X utf8 -c 'import sys, unittest; sys.path[:0] = ["src", "tests"]; r = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover("tests")); sys.exit(not r.wasSuccessful() or bool(r.skipped))'
```

在 Windows x64、Python 3.11 或更高版本下构建：

```powershell
python packaging/build_windows.py --output-dir dist --cache-dir build/dependency-cache
```

已有缓存时增加 `--offline`。依赖按 `packaging/windows-dependencies.json` 校验 SHA-256；构建只收集活动 `src/video_localizer` 的顶层 Python/配置窗口文件，随后用包内 Python 导入全部模块。历史目录、服务器部署、用户视频、译文及密钥不进入安装包。

自有代码使用条款见 `LICENSE.txt`，第三方组件按随包许可使用。
