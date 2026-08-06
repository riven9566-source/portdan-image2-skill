#!/usr/bin/env python3
"""Generate one PNG through the active Portdan Responses provider."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import secrets
import stat
import sys
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # Python 3.9/3.10
    tomllib = None  # type: ignore


HOST = "portdan.com"
ENDPOINT = "https://portdan.com/v1/responses"
IMAGE_MODEL = "gpt-image-2"
MISSING_KEY_MESSAGE = "请配置好 Portdan 后台的 API 密钥"
SIZES = ("1024x1024", "1536x1024", "1024x1536")
MAX_CONFIG_BYTES = 2 * 1024 * 1024
MAX_SETTINGS_BYTES = 512 * 1024
MAX_PROMPT_CHARS = 20_000
MAX_PROMPT_BYTES = 80_000
MAX_RESPONSE_BYTES = 96 * 1024 * 1024
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_IMAGE_PIXELS = 4_000_000


class ConfigError(RuntimeError):
    pass


class RequestError(RuntimeError):
    def __init__(self, status: int = 0) -> None:
        self.status = status
        super().__init__(str(status))


class ResponseError(RuntimeError):
    pass


class OutputRecoveryError(RuntimeError):
    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(str(path))


@dataclass(frozen=True)
class Provider:
    model: str
    api_key: str = field(repr=False)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _is_link_like(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _read_regular(path: Path, limit: int) -> bytes:
    descriptor = -1
    try:
        info = path.lstat()
    except OSError as exc:
        raise ConfigError() from exc
    if _is_link_like(info) or not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ConfigError()
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(str(path), flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > limit:
            raise ConfigError()
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            data = handle.read(limit + 1)
    except OSError as exc:
        raise ConfigError() from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(data) > limit:
        raise ConfigError()
    return data


def _directory(path: Path) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ConfigError() from exc
    if _is_link_like(info) or not stat.S_ISDIR(info.st_mode):
        raise ConfigError()
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ConfigError() from exc
    if resolved == Path(resolved.anchor):
        raise ConfigError()
    return resolved


def _config_root() -> Path:
    home = Path.home()
    settings_path = home / ".cc-switch" / "settings.json"
    if settings_path.exists() or settings_path.is_symlink():
        try:
            settings = json.loads(_read_regular(settings_path, MAX_SETTINGS_BYTES).decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ConfigError() from exc
        if not isinstance(settings, dict):
            raise ConfigError()
        custom = settings.get("codexConfigDir")
        if custom is not None:
            if not isinstance(custom, str) or not custom.strip():
                raise ConfigError()
            custom_path = Path(custom.strip()).expanduser()
            if not custom_path.is_absolute():
                raise ConfigError()
            return _directory(custom_path)
    return _directory(home / ".codex")


def _strip_comment(line: str) -> str:
    quote = ""
    escaped = False
    for index, character in enumerate(line):
        if quote == '"' and escaped:
            escaped = False
            continue
        if quote == '"' and character == "\\":
            escaped = True
            continue
        if character in ('"', "'"):
            if not quote:
                quote = character
            elif quote == character:
                quote = ""
        elif character == "#" and not quote:
            return line[:index]
    return line


def _toml_value(raw: str) -> Any:
    value = raw.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        try:
            return json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError() from exc
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1]
    if value == "true":
        return True
    if value == "false":
        return False
    return value


def _fallback_toml(text: str) -> Dict[str, Any]:
    top: Dict[str, Any] = {}
    providers: Dict[str, Dict[str, Any]] = {}
    current: Optional[str] = None
    for original in text.splitlines():
        line = _strip_comment(original).strip()
        if not line:
            continue
        table = re.fullmatch(
            r'\[model_providers\.(?:"([^"\\]+)"|\'([^\'\\]+)\'|([A-Za-z0-9_-]+))\]',
            line,
        )
        if table:
            current = next(value for value in table.groups() if value is not None)
            providers.setdefault(current, {})
            continue
        if line.startswith("["):
            current = None
            continue
        if "=" not in line:
            continue
        key, raw = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            continue
        value = _toml_value(raw)
        dotted = re.fullmatch(r"model_providers\.([A-Za-z0-9_-]+)\.(base_url|wire_api|experimental_bearer_token)", key)
        if dotted:
            providers.setdefault(dotted.group(1), {})[dotted.group(2)] = value
        elif current is None:
            if key in ("model", "model_provider"):
                top[key] = value
        elif key in ("base_url", "wire_api", "experimental_bearer_token"):
            providers[current][key] = value
    top["model_providers"] = providers
    return top


def _parse_config(raw: bytes) -> Dict[str, Any]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConfigError() from exc
    try:
        if tomllib is not None:
            value = tomllib.loads(text)
        else:
            value = _fallback_toml(text)
    except Exception as exc:
        raise ConfigError() from exc
    if not isinstance(value, dict):
        raise ConfigError()
    return value


def _endpoint(base_url: Any) -> str:
    if not isinstance(base_url, str):
        raise ConfigError()
    try:
        parsed = urlsplit(base_url.strip())
        port = parsed.port
    except (TypeError, ValueError, AttributeError) as exc:
        raise ConfigError() from exc
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != HOST
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") not in ("", "/v1")
    ):
        raise ConfigError()
    return ENDPOINT


def _key(value: Any) -> str:
    if not isinstance(value, str):
        raise ConfigError()
    result = value.strip()
    if result.lower().startswith("bearer "):
        result = result[7:].strip()
    if not result or len(result) > 8192 or any(ord(c) < 32 or c.isspace() for c in result):
        raise ConfigError()
    return result


def resolve_provider() -> Provider:
    root = _config_root()
    config = _parse_config(_read_regular(root / "config.toml", MAX_CONFIG_BYTES))
    active = config.get("model_provider")
    providers = config.get("model_providers")
    if not isinstance(active, str) or not isinstance(providers, dict):
        raise ConfigError()
    provider = providers.get(active)
    if not isinstance(provider, dict):
        raise ConfigError()
    if provider.get("wire_api") != "responses":
        raise ConfigError()
    _endpoint(provider.get("base_url"))
    model = config.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ConfigError()
    if "experimental_bearer_token" in provider:
        return Provider(model=model.strip(), api_key=_key(provider.get("experimental_bearer_token")))
    raise ConfigError()


def _payload(provider: Provider, prompt: str, size: str) -> bytes:
    body = {
        "model": provider.model,
        "input": prompt,
        "tools": [{
            "type": "image_generation",
            "action": "generate",
            "model": IMAGE_MODEL,
            "size": size,
            "output_format": "png",
        }],
        "tool_choice": {"type": "image_generation"},
        "store": False,
        "stream": False,
    }
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _post(provider: Provider, body: bytes, timeout: float) -> bytes:
    request = Request(
        ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + provider.api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "portdan-image2-skill/2.0",
        },
    )
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            status = int(getattr(response, "status", response.getcode()))
            data = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise RequestError(int(exc.code)) from None
    except (OSError, URLError, TimeoutError):
        raise RequestError(0) from None
    if len(data) > MAX_RESPONSE_BYTES or not 200 <= status < 300:
        raise RequestError(status)
    return data


def _image_bytes(raw: bytes) -> bytes:
    try:
        response = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ResponseError() from exc
    output = response.get("output") if isinstance(response, dict) else None
    if not isinstance(output, list):
        raise ResponseError()
    calls = [item for item in output if isinstance(item, dict) and item.get("type") == "image_generation_call"]
    if len(calls) != 1 or not isinstance(calls[0].get("result"), str):
        raise ResponseError()
    value = calls[0]["result"]
    if value.startswith("data:") and "," in value:
        value = value.split(",", 1)[1]
    try:
        data = base64.b64decode(re.sub(r"\s+", "", value), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ResponseError() from exc
    if len(data) > MAX_IMAGE_BYTES:
        raise ResponseError()
    return _sanitize_png(data)


def _sanitize_png(data: bytes) -> bytes:
    if len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ResponseError()
    offset = 8
    seen_ihdr = False
    seen_idat = False
    seen_plte = False
    cleaned = [data[:8]]
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset:offset + 4], "big")
        end = offset + 12 + length
        if length > MAX_IMAGE_BYTES or end > len(data):
            raise ResponseError()
        kind = data[offset + 4:offset + 8]
        chunk = data[offset + 8:offset + 8 + length]
        checksum = data[offset + 8 + length:end]
        if zlib.crc32(kind + chunk) & 0xFFFFFFFF != int.from_bytes(checksum, "big"):
            raise ResponseError()
        if kind == b"IHDR":
            if length != 13:
                raise ResponseError()
            width = int.from_bytes(chunk[:4], "big")
            height = int.from_bytes(chunk[4:8], "big")
            color_type = chunk[9]
            if (
                seen_ihdr
                or not width
                or not height
                or width * height > MAX_IMAGE_PIXELS
                or chunk[8] not in (1, 2, 4, 8, 16)
                or color_type not in (0, 2, 3, 4, 6)
                or chunk[10] != 0
                or chunk[11] != 0
                or chunk[12] not in (0, 1)
            ):
                raise ResponseError()
            seen_ihdr = True
            cleaned.append(data[offset:end])
        elif kind == b"PLTE":
            if not seen_ihdr or seen_idat or seen_plte or not length or length % 3:
                raise ResponseError()
            seen_plte = True
            cleaned.append(data[offset:end])
        elif kind == b"IDAT":
            if not seen_ihdr:
                raise ResponseError()
            seen_idat = seen_idat or length > 0
            cleaned.append(data[offset:end])
        elif kind == b"IEND":
            if (
                not seen_ihdr
                or not seen_idat
                or (color_type == 3 and not seen_plte)
                or length != 0
                or end != len(data)
            ):
                raise ResponseError()
            cleaned.append(data[offset:end])
            return b"".join(cleaned)
        elif not (kind[0] & 0x20):
            raise ResponseError()
        offset = end
    raise ResponseError()


def _save_png(data: bytes) -> Path:
    output = Path.cwd() / "portdan-images"
    output.mkdir(parents=True, exist_ok=True)
    info = output.lstat()
    if _is_link_like(info) or not stat.S_ISDIR(info.st_mode):
        raise OSError("output directory is invalid")
    directory = output.resolve(strict=True)
    if _is_link_like(directory.lstat()):
        raise OSError("output directory changed unexpectedly")
    staging: Optional[Path] = None
    for _ in range(20):
        candidate = directory / (".portdan-image2-stage-" + secrets.token_hex(8))
        try:
            os.mkdir(str(candidate), 0o700)
            info = candidate.lstat()
            if _is_link_like(info) or not stat.S_ISDIR(info.st_mode):
                raise OSError("staging directory is invalid")
            staging = candidate
            break
        except FileExistsError:
            continue
    if staging is None:
        raise OSError("could not allocate a private staging directory")

    temporary = staging / "image.png"
    descriptor = -1
    identity: Optional[tuple[int, int]] = None
    recovery = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(str(temporary), flags, 0o600)
        temp_info = os.fstat(descriptor)
        if not stat.S_ISREG(temp_info.st_mode):
            raise OSError("staging output is not a regular file")
        identity = (temp_info.st_dev, temp_info.st_ino)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("could not write output")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1

        current = temporary.lstat()
        if (
            not stat.S_ISREG(current.st_mode)
            or identity != (current.st_dev, current.st_ino)
            or current.st_size != len(data)
        ):
            raise OSError("staging output changed unexpectedly")
        for _ in range(20):
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
            final = directory / ("portdan-image-{}-{}.png".format(stamp, secrets.token_hex(8)))
            try:
                os.link(str(temporary), str(final), follow_symlinks=False)
            except FileExistsError:
                continue
            except OSError:
                recovery = True
                raise OutputRecoveryError(temporary.absolute()) from None
            try:
                _unlink_owned(temporary, identity)
                staging.rmdir()
            except OSError:
                pass
            return final
        recovery = True
        raise OutputRecoveryError(temporary.absolute())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not recovery:
            try:
                if identity is not None:
                    _unlink_owned(temporary, identity)
                staging.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _unlink_owned(path: Path, identity: tuple[int, int]) -> None:
    current = path.lstat()
    if stat.S_ISREG(current.st_mode) and identity == (current.st_dev, current.st_ino):
        path.unlink()


def _prompt_from_stdin() -> str:
    source = getattr(sys.stdin, "buffer", sys.stdin)
    raw = source.readline(MAX_PROMPT_BYTES + 1)
    if isinstance(raw, bytes):
        try:
            prompt = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError() from exc
    else:
        prompt = str(raw)
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ValueError()
    prompt = prompt.strip()
    if not prompt or len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError()
    return prompt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate one Portdan gpt-image-2 PNG")
    parser.add_argument("--prompt-stdin", action="store_true", help="read the visual prompt from standard input")
    parser.add_argument("--size", choices=SIZES, default="1024x1024")
    parser.add_argument("--timeout", type=float, default=300.0)
    return parser


def _error_message(error: RequestError) -> str:
    if error.status in (401, 403):
        return "Portdan API Key 无效或没有图片权限"
    if error.status == 429:
        return "Portdan 当前限流，请稍后再试"
    if error.status == 0 or error.status >= 500:
        return "Portdan 请求失败；请求可能已经到达后台，请先检查 Portdan 记录后再决定是否重试"
    return "Portdan 拒绝了图片请求，请检查当前模型和请求配置"


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if not args.prompt_stdin or args.timeout <= 0:
        print("图片提示词或超时时间无效", file=sys.stderr)
        return 2
    try:
        prompt = _prompt_from_stdin()
        provider = resolve_provider()
        body = _payload(provider, prompt, args.size)
        raw = _post(provider, body, min(args.timeout, 900.0))
        image = _image_bytes(raw)
        print(_save_png(image).absolute())
        return 0
    except ConfigError:
        print(MISSING_KEY_MESSAGE, file=sys.stderr)
        return 2
    except ValueError:
        print("图片提示词或超时时间无效", file=sys.stderr)
        return 2
    except RequestError as exc:
        print(_error_message(exc), file=sys.stderr)
        return 3 if exc.status in (401, 403) else 4
    except ResponseError:
        print("Portdan 返回的图片数据无效", file=sys.stderr)
        return 5
    except OutputRecoveryError as exc:
        print("无法安全发布图片；恢复文件保留在 {}".format(exc.path), file=sys.stderr)
        return 6
    except OSError:
        print("无法安全保存生成的图片", file=sys.stderr)
        return 6


if __name__ == "__main__":
    raise SystemExit(main())
