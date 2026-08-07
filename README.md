# Portdan Image2 Skill

让 Codex 通过 Portdan Responses API 调用 OpenAI `gpt-image-2` 生成一张 PNG。

- OpenAI 提供真正的 `gpt-image-2` 生图模型。
- Portdan 提供 API 接入与计费通道。
- Skill 自带的 Python 只负责安全取得 Portdan Key、发送一次请求并保存图片。

因此它不是图库拼接、盗版图片或本地仿制模型，也不会消耗 Codex/ChatGPT
账号自带的生图额度。

## 安装

只需要 Python 3.9+，不需要安装 Python 包。安装器不会修改 Codex、CC
Switch 配置或登录状态。

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

没有 Git 时可在 GitHub 下载 ZIP，解压后运行同样的安装命令。安装完成后
重启 Codex 或新建会话。

## 使用

直接指定画质会立即生成：

```text
使用 $portdan-image2 快速生成一张正方形图片：一只金毛幼犬坐在阳光草地上，写实摄影，无文字、无 logo、无水印。
```

支持三档画质：

- 快速：`low`
- 均衡：`medium`
- 高清：`high`

如果没有指定画质，Skill 只问一次：

```text
请选择画质：快速、均衡还是高清？
```

尺寸根据描述自动选择正方形、横版或竖版。成功后会显示：

```text
已通过 Portdan 调用 OpenAI gpt-image-2 生成
```

并返回 `portdan-images/` 中新图片的绝对路径和生成耗时。

## 固定请求链路

```text
自动读取或本次提供的 Portdan Key
  → POST https://portdan.com/v1/responses
  → Responses image_generation（OpenAI gpt-image-2）
  → output[].image_generation_call.result
  → 本地 PNG
```

请求固定为单图、非流式、一次提交：图片工具使用 `action=generate`、
`model=gpt-image-2` 和 PNG。外层 Responses 模型优先采用找到 Key 的同一份
Codex 配置中的兼容当前模型；没有模型或当前模型不支持图片工具时回退
`gpt-5.4-mini`。只有 Key 是必需的，不会探测模型、预检、轮询或自动重试。

## Key 如何读取

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
配置确认它指向 Portdan。因此供应商可以叫任意名字。

确认为 Portdan 后，运行器只读取明确的凭据字段：
`OPENAI_API_KEY`、`CODEX_API_KEY`、`API_KEY`、`api_key`、`apiKey`、
`experimental_bearer_token` 以及配置声明的 `env_key`。它不盲扫其他字符串，
也不会误用 OAuth 或刷新令牌。配置中有当前 Codex 模型时会一并采用，
没有也不影响生图。运行器保留安全的凭据来源标识，可说明 Key 来自
CC Switch、Codex 配置、`PORTDAN_API_KEY` 或用户本次提供，但绝不输出 Key。

### 自动读取失败时

运行器会明确说明本次尚未发送图片请求：

```text
未读取到可用于 Portdan 的 API Key；本次没有发送图片请求。
```

用户可以在启动 Codex 的本机环境中设置 `PORTDAN_API_KEY`，也可明确提供
仅用于本次调用的 Key。后一种情况下，助手使用
`--prompt-stdin --api-key-stdin`，通过非 TTY stdin 严格传入两行：
第一行是图片提示词，第二行是 Key。运行器只在当前进程中临时设置
`PORTDAN_API_KEY`，完成后恢复原值。

Key 不得放进命令行、临时文件或 shell 配置，也不得回显、记录、写入
日志或最终报告。第一次因缺 Key 终止时根本没有调用 HTTP，所以不会
重复计费；用户提供 Key 后也只提交一次。

常见错误会保持简短：

- `401/403`：Portdan 拒绝认证，或当前分组未授权图片请求；
- `404`：Portdan 返回 404，图片请求未完成；
- `429`：Portdan 当前限流；
- 传输错误、超时或 `5xx`：Skill 会说明失败阶段并停止。

网络失败和 `5xx` 一律不自动重试；只有用户明确要求时才再提交。

## 为什么保留 Python

Codex 内置生图工具不能传入用户自己的 Portdan URL 和 Key，直接使用会走另一
条账号生图通道。Python 不是生图模型，只负责找 Key、发送 Responses 请求和
保存文件；真正生成图片的是通过 Portdan 调用的 OpenAI `gpt-image-2`。

## 本地开发验证

```bash
python3 -m unittest discover -s tests -v
python3 tools/validate_skill.py
python3 -m py_compile install.py package_skill.py skill_manifest.py tools/validate_skill.py skill/portdan-image2/scripts/generate_image.py
python3 install.py --dry-run
python3 package_skill.py --force
```

`.skill` 包只包含 Skill 运行所需的四个文件，不包含测试、Key、配置、图片或缓存。
