# Portdan Image2 Skill

> **先确认三个名称**
>
> - GitHub 仓库：`portdan-image2-skill`（包含数字 `2`）
> - Skill 名称：`portdan-image2`
> - 在 Codex 中调用：`$portdan-image2`
>
> 仓库根目录和 GitHub 的 **Download ZIP** 都是源码包，不是可直接导入的
> Skill 目录；根目录没有、也不会复制第二份 `SKILL.md`。交给
> `$skill-installer` 的正确地址是：
>
> ```text
> https://github.com/riven9566-source/portdan-image2-skill/tree/main/skill/portdan-image2
> ```

让 Codex 通过 Portdan 的 GPT-only `/v1/images/generations` 接口提交 GPT
Images-compatible 请求。Skill 是一层很薄的本机适配器：普通提示词只产生 `prompt` 这个
图片语义字段；用户明确给出的 API 字段（包括以后新增的字段）会保持原 JSON
类型透传；Python runner 只补齐流式传输与本地保存所需的两个字段。

- Portdan 提供 API 接入与计费通道；模型省略时由 Portdan API 按接口规则处理。
- Skill 不添加或承诺默认模型，也不能独立证明响应实际由哪个上游或模型实现生成。
- Skill 只调用 GPT 图片生成接口，不切换到 Grok、图片编辑接口或 Codex 内置生图。
- Runner 只负责取得用户自己的 Portdan Key、发送一次请求，并保存返回的
  PNG、JPEG、WebP 或未知格式的 `.bin` 制品。

它不会使用 Codex/ChatGPT 账号自带的生图额度。客户端能证明的是请求发送到
Portdan endpoint 并处理了返回响应；显式 `model` 字段本身不是上游实现证明。

## 适用范围与前置条件

这是面向支持本地 Skill 的 **Codex** 的个人 Key 工具，不是 ChatGPT 内置生图，
也不承诺兼容 Claude Code、Cursor 或 Gemini。使用者需要：

- 自己可用的 Portdan API Key，以及足够余额和图片模型权限；
- Python 3.9+ 和可写的本地输出目录；
- 能通过 HTTPS 访问 `portdan.com` 的网络。

默认采用直连，避免在不知情时把 Key 交给系统代理。用户明确要求时可使用系统
代理；PAC、SOCKS、需要额外认证的代理或 TLS 检查网络不保证兼容。Portdan 和
其上游的可用性、内容政策、延迟和最终账单也不由这个 Skill 保证。

## 安装

只需要 Python 3.9+，不需要安装 Python 包。安装器不会修改 Codex、CC
Switch 配置或登录状态。

### 方法一：使用 Codex Skill Installer（推荐）

在 Codex 中输入：

```text
请使用 $skill-installer 安装 https://github.com/riven9566-source/portdan-image2-skill/tree/main/skill/portdan-image2
```

不要只提供仓库根地址 `.../portdan-image2-skill`；Skill Installer 要求所选
目录的直接子文件是 `SKILL.md`。

### 方法二：从源码安装

macOS / Linux：

```bash
git clone https://github.com/riven9566-source/portdan-image2-skill.git
cd portdan-image2-skill
python3 install.py --dry-run
python3 install.py
```

Windows PowerShell：

```powershell
git clone https://github.com/riven9566-source/portdan-image2-skill.git
Set-Location .\portdan-image2-skill
py -3 .\install.py --dry-run
py -3 .\install.py
```

没有 Git 时，在仓库页面选择 **Code → Download ZIP**。解压得到的目录通常
名为 `portdan-image2-skill-main`，它仍然是源码仓库；进入该目录后运行：

macOS / Linux：

```bash
cd portdan-image2-skill-main
python3 install.py --dry-run
python3 install.py
```

Windows PowerShell：

```powershell
Set-Location .\portdan-image2-skill-main
py -3 .\install.py --dry-run
py -3 .\install.py
```

安装器优先使用显式的 `--codex-home`，其次使用 `CODEX_HOME`，再检查 CC
Switch 的自定义 Codex 目录，最后使用 `~/.codex`。`CODEX_HOME` 必须是绝对
路径。可先运行 `python3 install.py --version` 查看源码版本。

安装完成后重启 Codex 或新建会话。

### 方法三：未来的 GitHub Release 制品

[GitHub Releases](https://github.com/riven9566-source/portdan-image2-skill/releases)
在发布版本时将同时提供 `portdan-image2-<版本>.skill` 和同名 `.sha256`。`.skill` 将供支持
该格式导入的客户端或 Skill 管理工具使用的安装包；它和 GitHub 的源码 ZIP
不是同一种文件。下载后先在制品所在目录校验。

macOS：

```bash
shasum -a 256 -c portdan-image2-<版本>.skill.sha256
```

Linux：

```bash
sha256sum -c portdan-image2-<版本>.skill.sha256
```

Windows PowerShell：

```powershell
$expected = (Get-Content .\portdan-image2-<版本>.skill.sha256 -Raw).Trim().Split()[0]
$actual = (Get-FileHash .\portdan-image2-<版本>.skill -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $expected) { throw "SHA-256 校验失败" }
```

未来的 Release 包将由对应 Tag 的源码重新构建，不会从仓库中的旧 `dist`
文件发布。

### 升级

Git 用户先在源码仓库运行 `git pull --ff-only`；ZIP 用户重新下载并解压最新
源码。随后在新的源码目录执行：

```bash
python3 install.py --dry-run --upgrade
python3 install.py --upgrade
```

Windows 把 `python3` 换成 `py -3`。升级仍然只替换这个 Skill，不会修改
Codex 或 CC Switch 配置。

## 使用

普通提示词直接生成，不追问画质：

```text
使用 $portdan-image2 生成图片：未来城市上空的白色鲸鱼，蓝金配色。
```

这类请求的图片语义只有用户原始 `prompt`。Skill 不会自行添加 `model`、
`size`、`n`、`quality`、`output_format`，不会自动选择横竖版，也不会追加
`no readable text`、`no logo`、`no watermark` 等创意限制。模型省略时完全
交由 Portdan API 处理；Skill 不写入或承诺默认模型。

需要高级参数时，在请求中明确给出即可：

```text
使用 $portdan-image2 生成图片：model=gpt-image-future-preview，n=100，
partial_images=true，output_format=webp，并加入 future_render_mode={"passes":[1,null,3]}。
```

`model`、`n`、`quality`、`size`、`background`、`output_format`、
`output_compression`、`moderation`、`partial_images`、`style` 以及 Portdan
以后增加的未知字段，
都会保持字段名、JSON 类型和嵌套结构放进同一个请求。Skill 不对 `n` 另设
固定范围上限，也不截断、循环或拆分请求；字段和值最终是否受支持，由实际
Portdan API 响应决定。Runner 也不会依据 `n` 预分配结果槽、占位 artifact
或输出路径；本地状态只随实际返回的 artifact 增长。显式模型必须属于
`gpt-image-...` 家族；未来的
`gpt-image-...` 名称也不会因 Skill 未预先列举而被拒绝。此 Skill
不处理 Grok 或 `/v1/images/edits`。

### Canonical runner 调用

助手使用非 TTY stdin 调用：

```text
<python-command> <skill目录>/scripts/generate_image.py --request-json-stdin --json
```

stdin 是一个 JSON 对象。提示词和请求 JSON 不进入命令参数、环境变量、
heredoc、临时文件或 shell source。Runner 只覆盖两个传输字段：
`stream=true` 和 `response_format=b64_json`；其余字段原样透传。每次运行只发送
一个 `POST https://portdan.com/v1/images/generations`，不做预检、轮询或自动
重试。

`--json` 模式的 stdout 恰好包含一行终态 JSON，顶层字段固定：

```json
{"schema":"portdan-image2.result.v1","status":"completed","error":null,"request_id":"pdi-...","requested":null,"completed":1,"artifacts":[{"path":"/absolute/path/image.webp","format":"webp","bytes":12345}],"diagnostics":null,"elapsed_seconds":42.1}
```

- `status` 为 `completed`、`partial`、`error` 或 `diagnose`。
- `error` 为 `null` 或安全的 `{code, stage}`，不会包含 Key 或原始 provider 错误。
- `artifacts` 按到达顺序列出；每项包含绝对 `path`、`bytes`，以及 `png`、
  `jpeg`、`webp` 或 `bin` 格式。扩展名只根据实际字节 magic 决定：分别保存为
  `.png`、`.jpeg`、`.webp` 或未知格式的 `.bin`；不会依据 `output_format`、
  MIME/data URI metadata 或其他请求字段猜测格式。
- 只有输入 `n` 是正 JSON 整数且不是布尔值时，`requested` 才是该数字；`n`
  缺失、为零、负数、小数、字符串、布尔、数组、对象或 `null` 时均为
  `requested=null`。这只影响结果报告，不改变请求字段的原样透传。
- `completed` 是安全发布到本地的 artifact 数量；普通生成的
  `diagnostics=null`，包括 `completed`、`partial` 和 `error`。

完整成功时返回全部 artifact，但不把 `model` 参数表述成上游实现证明。若结果
是 `partial`，仍返回所有已保存文件：`requested` 为数字时准确说明
`completed/requested`；`requested=null` 时只报告 `completed`，不编造分母。
已完成图片可能已经计费；缺失图片不会被伪造或自动补发。再次提交可能增加
费用，必须由用户明确发起新请求。

### 等待、代理与一次提交

默认连接阶段 timeout 为 15 秒，收到响应后的 network idle timeout 为 1800 秒；
canonical 路径默认没有 overall deadline。只有显式传入兼容性 `--timeout` 时才会
增加 overall deadline。Runner 默认每 20 秒在 stderr 输出本地心跳和安全请求
ID；只有真实收到的网络字节会重置 network idle，心跳只表示同一本机进程仍在
等待。工具暂时 yield 或返回 session ID 时，助手必须恢复或等待同一会话，不能
启动第二个 runner。

默认 `--proxy-mode direct` 忽略系统代理。只有用户明确要求使用系统代理，或
直连失败后明确授权一次新的可能付费提交时，才使用 `--proxy-mode system`；
不得读取或打印代理地址和凭据。网络错误、超时、4xx、5xx、非法响应和部分
结果一律不自动重试。

### 本机诊断

只检查本机运行条件、不生成图片时使用：

```text
<python-command> <skill目录>/scripts/generate_image.py --diagnose --json
```

诊断 runner 只启动一次；不得自动重跑或另起第二个诊断进程。

诊断可以只读检查正常的本机配置和 credential candidates，只用于得到安全的
Key 来源 code；它绝不输出 Key 值。诊断不联网、不创建输出目录或图片，也不
修改配置。结果仍使用同一固定 schema，且为 `status="diagnose"`、`error=null`、
`request_id=null`、`requested=null`、`completed=0`、`artifacts=[]`，并包含
恰好由 `endpoint`、`key_source`、`output_directory` 组成的 `diagnostics`
对象。`output_directory` 只是将来生成时会使用的绝对路径，诊断不会创建它。
这些信息不能证明余额、图片权限、内容政策、限流余量、代理或 DNS/TLS
可达性，以及 Portdan 或其上游健康。

```json
{"schema":"portdan-image2.result.v1","status":"diagnose","error":null,"request_id":null,"requested":null,"completed":0,"artifacts":[],"diagnostics":{"endpoint":"https://portdan.com/v1/images/generations","key_source":"codex_home","output_directory":"/absolute/path/portdan-images"},"elapsed_seconds":0.01}
```

## 固定请求链路

```text
用户 prompt + 明确指定的 API 字段
  → runner 只覆盖 stream=true、response_format=b64_json
  → POST https://portdan.com/v1/images/generations（恰好一次）
  → 接收流式完成事件并验证 PNG/JPEG/WebP/未知格式字节
  → 安全发布本地 artifacts + 一行终态 JSON
```

这条链路不依赖当前 Codex 文本模型，也不会探测模型、预检 endpoint、轮询、
换 provider 或自动重试。

## Key 如何读取

优先在本机配置 Key，不要把 Key 粘贴到聊天、Issue、截图、命令参数或日志中。
聊天记录不是通用的秘密输入框，是否保留由用户所使用的客户端和服务决定。

运行器按以下顺序寻找 Key：

1. CC Switch 数据库中当前 Codex provider；
2. 当前已安装 Skill 所在的 Codex 目录；
3. `CODEX_HOME`；
4. CC Switch 的 `codexConfigDir`；
5. `~/.codex`；
6. `PORTDAN_API_KEY` 环境变量。

自动识别不依赖 provider 显示名或 Key 名包含 `Portdan`。运行器优先用
CC Switch `settings.json.currentProviderCodex` 选中当前 Codex provider，
`is_current` 只作兼容回退；再通过 provider URL 或实际 Codex TOML
配置确认它指向 Portdan。因此供应商可以叫任意名字。常见 provider URL 会
自动识别根地址 `https://portdan.com`、版本根地址 `https://portdan.com/v1`
和完整地址 `https://portdan.com/v1/images/generations`，三者都允许结尾 `/`。

确认为 Portdan 后，运行器只读取明确的凭据字段：
`OPENAI_API_KEY`、`CODEX_API_KEY`、`API_KEY`、`api_key`、`apiKey`、
`experimental_bearer_token` 以及配置声明的 `env_key`。它不盲扫其他字符串，
也不会误用 OAuth 或刷新令牌。运行器保留安全的凭据来源标识，可说明 Key 来自
CC Switch、Codex 配置、`PORTDAN_API_KEY` 或用户本次提供，但绝不输出 Key。

### 自动读取失败时

运行器会明确说明本次尚未发送图片请求：

```text
未读取到可用于 Portdan 的 API Key；本次没有发送图片请求。
```

默认做法是在启动 Codex 的本机环境中设置 `PORTDAN_API_KEY`，然后重新启动
Codex。不要默认要求用户把 Key 发进聊天。只有用户理解聊天可能保留秘密、仍
明确选择一次性方式时，助手才使用
`--request-json-stdin --api-key-stdin --json`，通过非 TTY stdin 严格传入
两行且不允许多余输入：第一行是 compact JSON 请求对象，第二行是 Key。
运行器只在当前进程内存中保留这枚一次性 Key，不会把它导出到进程环境或
写入任何配置。

即便使用一次性 stdin，Key 也不得放进命令行、临时文件或 shell 配置，不得
回显、记录、写入日志或最终报告。第一次因缺 Key 终止时根本没有调用 HTTP，
所以不会产生图片费用；用户明确选择一次性方式后也只提交一次。

常见错误只返回安全的 `code` 和 `stage`，不会原样转发 provider 响应。缺少
Python、非法 JSON、不属于 GPT Images 的显式模型或不安全的本机资源条件，
应尽可能在 POST 前停止。缺 Key 会明确说明没有发送图片请求；认证、余额、
图片权限、内容政策、字段或值支持、限流、DNS、TLS、代理、上游状态、超时、
响应格式和本地磁盘仍可能在诊断后失败。

请求一旦到达 Portdan，即使客户端随后超时、断线、拒绝 artifact 或只取得
部分结果，也可能已经产生费用。空 artifact 列表本身不能证明上游没有计费。
任何失败或 `partial` 都不自动重试；只有用户明确要求时才发起新的请求。

## 为什么保留 Python

Codex 内置生图工具不能传入用户自己的 Portdan URL 和 Key，直接使用会走另一
条账号生图通道。Python 不是生图模型，只负责找 Key、薄透传一次 GPT
Images-compatible 请求、根据实际 magic 保存字节，并输出本地 artifacts 与
结构化 JSON；它不独立证明 Portdan 实际选择的上游实现。

## 本地开发验证

```bash
python3 -m unittest discover -s tests -v
python3 tools/validate_skill.py
python3 -m py_compile install.py package_skill.py skill_manifest.py tools/validate_skill.py skill/portdan-image2/scripts/generate_image.py
python3 install.py --dry-run
python3 package_skill.py --output-dir build/release
```

PR 和 `main` push 的 CI 在 Ubuntu、macOS、Windows 上分别使用 Python 3.9
和当前 3.x 运行同一套离线测试、validator、编译与制品一致性检查；CI 不接收
Portdan Key，也不执行真实生图。`evals/evals.json` 只描述 Codex 行为验收，
不会自动产生付费请求。

打包器根据 `skill_manifest.py` 中的版本生成带版本号的 `.skill` 和对应
`.sha256`，并逐文件验证包内容与当前源码一致。仓库不再提交 `dist` 制品；
未来正式发布时，制品将只由版本 Tag 的 Release 工作流重新构建。`.skill`
将只包含 Skill 运行所需的四个文件，不包含测试、Key、配置、图片或缓存。
