# Portdan Image2 Skill

让 Codex 通过 Portdan Responses API 调用 OpenAI `gpt-image-2` 生成一张 PNG。

- OpenAI 提供真正的 `gpt-image-2` 生图模型。
- Portdan 提供 API 接入与计费通道。
- Skill 自带的 Python 只负责自动找到 Portdan Key、发送一次请求并保存图片。

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
本机已有的 Portdan Key
  → POST https://portdan.com/v1/responses
  → Responses image_generation（OpenAI gpt-image-2）
  → output[].image_generation_call.result
  → 本地 PNG
```

请求固定为单图、非流式、一次提交：图片工具使用 `action=generate`、
`model=gpt-image-2` 和 PNG。外层 Responses 模型优先采用找到 Key 的同一份
Codex 配置中的兼容当前模型；没有模型或当前模型不支持图片工具时回退
`gpt-5.4-mini`。只有 Key 是必需的，不会探测模型、预检、轮询或自动重试。

## Key 如何自动读取

运行器按以下顺序寻找 Key：

1. CC Switch 数据库中当前 Codex provider；
2. 当前已安装 Skill 所在的 Codex 目录；
3. `CODEX_HOME`；
4. CC Switch 的 `codexConfigDir`；
5. `~/.codex`；
6. `PORTDAN_API_KEY` 环境变量。

兼容 CC Switch provider 数据、`experimental_bearer_token`、`env_key`，以及
`openai_base_url` / `requires_openai_auth` 配合 `auth.json` 中
`OPENAI_API_KEY` 的常见配置。无需完整的 `model_provider`、`model`、
`wire_api` 或可变 Base URL。配置中有当前 Codex 模型时会一并采用，没有也不
影响生图。

找不到 Key 时只提示：

```text
未找到 Portdan API Key，请先在 CC Switch 中选择 Portdan，或设置 PORTDAN_API_KEY
```

常见错误会保持简短：

- `401/403`：Portdan 拒绝认证，或当前分组未授权图片请求；
- `404`：Portdan 返回 404，图片请求未完成；
- `429`：Portdan 当前限流；
- 超时或 `5xx`：请求可能已到达后台，Skill 不会自动重复提交。

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
