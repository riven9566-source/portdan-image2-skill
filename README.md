# Portdan Image2 Skill

让 Codex 使用 CC Switch 当前启用的 Portdan API Key，通过 Portdan Responses API 生成一张 `gpt-image-2` 图片。

这个项目不是“让模型猜怎么读取 Key”的提示词。真正的 Key 读取和 HTTP 请求由 Skill 自带的本地 Python 执行器完成，因此不会走 Codex/ChatGPT 账号生图。

## 安装

项目只依赖 Python 标准库，不需要 `pip install`。需要 Python 3.9+；安装器不会静默安装软件、修改 PATH 或修改任何 Codex / CC Switch 配置。

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

安装器只写入当前 Codex Skill 目录下的 `skills/portdan-image2`，不会修改 `config.toml`、`auth.json`、CC Switch 设置或登录状态。安装后重启 Codex 或新建会话。

如果没有 Python，请从 Python 官网安装 Python 3.9 或更高版本；本项目不会静默安装或修改 PATH。

上述方式需要 Git。没有 Git 时，在本仓库 GitHub 页面选择 **Code → Download ZIP**，解压后在该目录运行同样的 Python 命令即可。

## 唯一推荐用法

在 Codex 中发送：

```text
使用 $portdan-image2 生成一张正方形图片：一只金毛幼犬坐在阳光草地上，写实摄影，无文字、无 logo、无水印。
```

也可以替换成自己的画面描述，例如：

```text
使用 $portdan-image2 生成一张横版电影感图片：雨夜城市街头，前景一把红伞，湿地面反射青色和琥珀色霓虹，低机位，无文字、无 logo、无水印。
```

Skill 会把描述整理成视觉提示词，调用一次 Portdan API，并在当前工作目录的 `portdan-images/` 中创建一个新的 PNG。成功时返回绝对路径；Codex 客户端支持本地图片展示时也会展示图片。尺寸是发送给服务端的请求参数，Portdan 可能调整最终像素尺寸；只要返回有效 PNG 即视为成功，不会因此自动重试。

## 固定请求链路

```text
CC Switch 当前 Codex 配置
  → 活动 provider 的 API Key 和 base_url
  → POST https://portdan.com/v1/responses
  → 活动配置中的外层 model（当前示例为 gpt-5.6-sol）
  → image_generation / gpt-image-2
  → image_generation_call.result
  → 本地 PNG
```

固定行为：一次请求、同步返回、不查询额度、不记录用量、不轮询、不自动重试、不切换账号或 provider。

## Key 如何自动读取

运行器按以下顺序定位配置目录：

1. `~/.cc-switch/settings.json` 中非空的 `codexConfigDir`；
2. 没有自定义目录时使用 `~/.codex`。

运行器需要加载该目录的 `config.toml` 来定位 `model_provider`，但只会选择、使用和发送活动 provider 的静态 `experimental_bearer_token`。不会把其他 provider 的凭据用于请求或输出；也不会读取环境变量、`auth.json`、OAuth/account token 或单独的 `PORTDAN_API_KEY`。

如果读取不到可用的 Portdan Base URL 或 API Key，Skill 只提示：

```text
请配置好 Portdan 后台的 API 密钥
```

不要把 Key 粘贴到聊天、命令参数、Issue 或截图中。

## 安全边界

- 只接受 Portdan HTTPS 地址并固定请求 `/v1/responses`。
- 不使用 Codex 内置 `image_gen` 或其他 provider。
- 不读取或修改浏览器、Shell 历史、系统钥匙串、代理、证书和登录文件。
- 不输出 Key、Authorization、Base64、原始响应或服务端完整错误。
- 输出文件使用新文件名并以排他方式创建，不覆盖已有图片。
- 远端错误不会自动重试；超时或 5xx 后停止，由用户自行决定是否再次生成。

## 本地开发验证

```bash
python3 -m unittest discover -s tests -v
python3 tools/validate_skill.py
python3 -m py_compile install.py package_skill.py skill_manifest.py tools/validate_skill.py skill/portdan-image2/scripts/generate_image.py
python3 package_skill.py --force
```

`.skill` 包只包含 Skill 运行所需的四个文件，不包含测试、配置、密钥、图片或构建缓存。
