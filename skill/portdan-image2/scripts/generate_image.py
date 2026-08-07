#!/usr/bin/env python3
"""Generate one PNG with OpenAI gpt-image-2 through Portdan Responses API."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import secrets
import sqlite3
import stat
import sys
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional
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
DEFAULT_RESPONSE_MODEL = "gpt-5.4-mini"
MISSING_KEY_MESSAGE = "未找到 Portdan API Key，请先在 CC Switch 中选择 Portdan，或设置 PORTDAN_API_KEY"
SIZES = ("1024x1024", "1536x1024", "1024x1536")
QUALITIES = ("low", "medium", "high")
QUALITY_LABELS = {"low": "快速", "medium": "均衡", "high": "高清"}
MAX_CONFIG_BYTES = 2 * 1024 * 1024
MAX_SETTINGS_BYTES = 512 * 1024
MAX_PROMPT_CHARS = 20_000
MAX_PROMPT_BYTES = 80_000
MAX_RESPONSE_BYTES = 96 * 1024 * 1024
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_IMAGE_PIXELS = 4_000_000
CC_SWITCH_DB_TIMEOUT_SECONDS = 0.2


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
class RequestConfig:
    api_key: str = field(repr=False)
    model: str = DEFAULT_RESPONSE_MODEL


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _is_link_like(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _read_small_file(path: Path, limit: int) -> bytes:
    try:
        resolved = path.expanduser().resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise ConfigError() from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ConfigError()
    try:
        with resolved.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        raise ConfigError() from exc
    if len(data) > limit:
        raise ConfigError()
    return data


def _existing_directory(path: Path) -> Optional[Path]:
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_dir() or resolved == Path(resolved.anchor):
            return None
    except OSError:
        return None
    return resolved


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
        dotted = re.fullmatch(
            r"model_providers\.([A-Za-z0-9_-]+)\."
            r"(base_url|wire_api|experimental_bearer_token|env_key|requires_openai_auth)",
            key,
        )
        if dotted:
            providers.setdefault(dotted.group(1), {})[dotted.group(2)] = value
        elif current is None:
            if key in ("model", "model_provider", "openai_base_url"):
                top[key] = value
        elif key in (
            "base_url",
            "wire_api",
            "experimental_bearer_token",
            "env_key",
            "requires_openai_auth",
        ):
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


def _is_portdan_url(base_url: Any) -> bool:
    if not isinstance(base_url, str):
        return False
    try:
        parsed = urlsplit(base_url.strip())
        port = parsed.port
    except (TypeError, ValueError, AttributeError):
        return False
    return not (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != HOST
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") not in ("", "/v1", "/backend-api/codex")
    )


def _key(value: Any) -> str:
    if not isinstance(value, str):
        raise ConfigError()
    result = value.strip()
    if result.lower().startswith("bearer "):
        result = result[7:].strip()
    if not result or len(result) > 8192 or any(ord(c) < 32 or c.isspace() for c in result):
        raise ConfigError()
    return result


def _maybe_key(value: Any) -> Optional[str]:
    try:
        return _key(value)
    except ConfigError:
        return None


def _json_object(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _auth_key(auth: Any) -> Optional[str]:
    payload = _json_object(auth)
    if payload is None:
        return None
    return _maybe_key(payload.get("OPENAI_API_KEY"))


def _configured_model(config: Any) -> str:
    if not isinstance(config, dict):
        return DEFAULT_RESPONSE_MODEL
    value = config.get("model")
    if not isinstance(value, str):
        return DEFAULT_RESPONSE_MODEL
    model = value.strip()
    if (
        not model
        or len(model) > 256
        or any(ord(character) < 33 or character.isspace() for character in model)
    ):
        return DEFAULT_RESPONSE_MODEL
    normalized = re.sub(r"[^a-z0-9]+", "", model.casefold())
    if "gpt-image-" in model.casefold() or "gpt53codexspark" in normalized:
        return DEFAULT_RESPONSE_MODEL
    return model


def _configured_providers(config: Dict[str, Any]) -> list[tuple[str, Dict[str, Any]]]:
    active = config.get("model_provider")
    providers = config.get("model_providers")
    if not isinstance(providers, dict):
        return []
    names = list(providers)
    if isinstance(active, str) and active in providers:
        names.remove(active)
        names.insert(0, active)
    result: list[tuple[str, Dict[str, Any]]] = []
    for name in names:
        provider = providers.get(name)
        if isinstance(provider, dict):
            result.append((name, provider))
    return result


def _provider_is_portdan(name: str, provider: Dict[str, Any]) -> bool:
    return "portdan" in name.casefold() or _is_portdan_url(provider.get("base_url"))


def _top_level_portdan_applies(
    config: Dict[str, Any], providers: list[tuple[str, Dict[str, Any]]]
) -> bool:
    if not _is_portdan_url(config.get("openai_base_url")):
        return False
    active = config.get("model_provider")
    if not isinstance(active, str):
        return True
    active_provider = next(
        ((name, provider) for name, provider in providers if name == active), None
    )
    return active_provider is None or _provider_is_portdan(*active_provider)


def _config_auth_allowed(config: Dict[str, Any], providers: list[tuple[str, Dict[str, Any]]]) -> bool:
    if _top_level_portdan_applies(config, providers):
        return True
    active = config.get("model_provider")
    if isinstance(active, str):
        active_provider = next(
            ((name, provider) for name, provider in providers if name == active), None
        )
        if active_provider is not None and _provider_is_portdan(*active_provider):
            return True
    return len(providers) == 1 and _provider_is_portdan(*providers[0])


def _key_from_provider(
    provider: Dict[str, Any], environ: Mapping[str, str]
) -> Optional[str]:
    inline = _maybe_key(provider.get("experimental_bearer_token"))
    if inline:
        return inline
    env_name = provider.get("env_key")
    if isinstance(env_name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
        return _maybe_key(environ.get(env_name))
    return None


def _key_from_config(
    config: Dict[str, Any], auth: Any, environ: Mapping[str, str]
) -> Optional[str]:
    request_config = _request_config_from_config(config, auth, environ)
    return request_config.api_key if request_config is not None else None


def _request_config_from_config(
    config: Dict[str, Any], auth: Any, environ: Mapping[str, str]
) -> Optional[RequestConfig]:
    providers = _configured_providers(config)
    model = _configured_model(config)
    active = config.get("model_provider")
    active_provider = next(
        (
            (name, provider)
            for name, provider in providers
            if isinstance(active, str) and name == active and _provider_is_portdan(name, provider)
        ),
        None,
    )
    if active_provider is not None:
        _, provider = active_provider
        direct = _key_from_provider(provider, environ)
        if direct:
            return RequestConfig(api_key=direct, model=model)
        auth_key = _auth_key(auth)
        if auth_key:
            return RequestConfig(api_key=auth_key, model=model)

    top_level_portdan = _top_level_portdan_applies(config, providers)
    if top_level_portdan:
        auth_key = _auth_key(auth)
        if auth_key:
            return RequestConfig(api_key=auth_key, model=model)

    candidates: set[str] = set()
    portdan_providers = [
        (name, provider)
        for name, provider in providers
        if _provider_is_portdan(name, provider)
    ]
    for _, provider in portdan_providers:
        direct = _key_from_provider(provider, environ)
        if direct:
            candidates.add(direct)
    if len(candidates) == 1:
        selected_model = (
            model
            if top_level_portdan or (len(providers) == 1 and len(portdan_providers) == 1)
            else DEFAULT_RESPONSE_MODEL
        )
        return RequestConfig(api_key=next(iter(candidates)), model=selected_model)
    if len(providers) == 1 and len(portdan_providers) == 1:
        auth_key = _auth_key(auth)
        if auth_key:
            return RequestConfig(api_key=auth_key, model=model)
    return None


def _read_auth(path: Path) -> Optional[Dict[str, Any]]:
    try:
        raw = _read_small_file(path, MAX_SETTINGS_BYTES)
        return _json_object(raw.decode("utf-8-sig"))
    except (ConfigError, UnicodeDecodeError):
        return None


def _request_config_from_config_root(
    root: Path, environ: Mapping[str, str]
) -> Optional[RequestConfig]:
    try:
        config = _parse_config(_read_small_file(root / "config.toml", MAX_CONFIG_BYTES))
    except ConfigError:
        return None
    model = _configured_model(config)
    providers = _configured_providers(config)
    active = config.get("model_provider")
    active_provider = next(
        (
            provider
            for name, provider in providers
            if isinstance(active, str)
            and name == active
            and _provider_is_portdan(name, provider)
        ),
        None,
    )
    if active_provider is not None:
        direct = _key_from_provider(active_provider, environ)
        if direct:
            return RequestConfig(api_key=direct, model=model)
    elif not _top_level_portdan_applies(config, providers) and len(providers) == 1:
        name, provider = providers[0]
        if _provider_is_portdan(name, provider):
            direct = _key_from_provider(provider, environ)
            if direct:
                return RequestConfig(api_key=direct, model=model)
    auth = _read_auth(root / "auth.json") if _config_auth_allowed(config, providers) else None
    return _request_config_from_config(config, auth, environ)


def _key_from_config_root(root: Path, environ: Mapping[str, str]) -> Optional[str]:
    request_config = _request_config_from_config_root(root, environ)
    return request_config.api_key if request_config is not None else None


def _request_config_from_cc_switch_database(
    home: Path, environ: Mapping[str, str]
) -> Optional[RequestConfig]:
    database = home / ".cc-switch" / "cc-switch.db"
    try:
        resolved = database.resolve(strict=True)
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode):
            return None
        connection = sqlite3.connect(
            resolved.as_uri() + "?mode=ro",
            uri=True,
            timeout=CC_SWITCH_DB_TIMEOUT_SECONDS,
        )
    except (OSError, sqlite3.Error, ValueError):
        return None
    try:
        connection.execute("PRAGMA query_only = ON")
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(providers)").fetchall()
            if len(row) > 1
        }
        if not {"settings_config", "app_type", "is_current"}.issubset(columns):
            return None
        identity_columns = [
            name for name in ("name", "website_url", "provider_type") if name in columns
        ]
        selected = ", ".join(["settings_config"] + identity_columns)
        rows = connection.execute(
            "SELECT " + selected + " FROM providers "
            "WHERE app_type = 'codex' AND is_current = 1 ORDER BY rowid DESC"
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    for row in rows:
        settings = _json_object(row[0] if row else None)
        if settings is None:
            continue
        config_text = settings.get("config")
        config: Optional[Dict[str, Any]] = None
        if isinstance(config_text, str):
            try:
                config = _parse_config(config_text.encode("utf-8"))
            except (ConfigError, UnicodeEncodeError):
                config = None
        model = _configured_model(config)
        identity = dict(zip(identity_columns, row[1:]))
        identity_is_portdan = (
            isinstance(identity.get("name"), str)
            and "portdan" in identity["name"].casefold()
        ) or _is_portdan_url(identity.get("website_url"))
        if identity_is_portdan:
            key = _auth_key(settings.get("auth"))
            if key:
                return RequestConfig(api_key=key, model=model)
        if config is not None and _config_auth_allowed(
            config, _configured_providers(config)
        ):
            key = _auth_key(settings.get("auth"))
            if key:
                return RequestConfig(api_key=key, model=model)
        if config is not None:
            request_config = _request_config_from_config(config, None, environ)
            if request_config is not None:
                return request_config
    return None


def _key_from_cc_switch_database(home: Path, environ: Mapping[str, str]) -> Optional[str]:
    request_config = _request_config_from_cc_switch_database(home, environ)
    return request_config.api_key if request_config is not None else None


def _custom_codex_root(home: Path) -> Optional[Path]:
    try:
        settings = _json_object(
            _read_small_file(home / ".cc-switch" / "settings.json", MAX_SETTINGS_BYTES).decode("utf-8-sig")
        )
    except (ConfigError, UnicodeDecodeError):
        return None
    custom = settings.get("codexConfigDir") if settings else None
    if not isinstance(custom, str) or not custom.strip():
        return None
    return _existing_directory(Path(custom.strip()).expanduser())


def _candidate_config_roots(
    home: Path, environ: Mapping[str, str]
) -> Iterator[Path]:
    seen: set[str] = set()

    def resolve(candidate: Path) -> Optional[Path]:
        resolved = _existing_directory(candidate)
        identity = str(resolved) if resolved is not None else ""
        if resolved is None or identity in seen:
            return None
        seen.add(identity)
        return resolved

    script = Path(__file__).resolve()
    if len(script.parents) > 3 and script.parents[2].name == "skills":
        installed = resolve(script.parents[3])
        if installed is not None:
            yield installed

    codex_home = environ.get("CODEX_HOME")
    if codex_home:
        configured = resolve(Path(codex_home).expanduser())
        if configured is not None:
            yield configured

    custom = _custom_codex_root(home)
    if custom is not None and str(custom) not in seen:
        seen.add(str(custom))
        yield custom

    default = resolve(home / ".codex")
    if default is not None:
        yield default


def resolve_request_config() -> RequestConfig:
    home = Path.home()
    environ = os.environ
    request_config = _request_config_from_cc_switch_database(home, environ)
    if request_config is not None:
        return request_config
    for root in _candidate_config_roots(home, environ):
        request_config = _request_config_from_config_root(root, environ)
        if request_config is not None:
            return request_config
    key = _maybe_key(environ.get("PORTDAN_API_KEY"))
    if key:
        return RequestConfig(api_key=key)
    raise ConfigError()


def resolve_api_key() -> str:
    return resolve_request_config().api_key


def _payload(request_config: RequestConfig, prompt: str, size: str, quality: str) -> bytes:
    body = {
        "model": request_config.model,
        "input": prompt,
        "tools": [{
            "type": "image_generation",
            "action": "generate",
            "model": IMAGE_MODEL,
            "quality": quality,
            "size": size,
            "output_format": "png",
        }],
        "tool_choice": {"type": "image_generation"},
        "store": False,
        "stream": False,
    }
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _post(api_key: str, body: bytes, timeout: float) -> bytes:
    request = Request(
        ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "portdan-image2-skill/4.0",
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
    calls = [
        item
        for item in output
        if isinstance(item, dict) and item.get("type") == "image_generation_call"
    ]
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
    parser.add_argument("--quality", choices=QUALITIES, default="medium")
    parser.add_argument("--timeout", type=float, default=300.0)
    return parser


def _error_message(error: RequestError) -> str:
    if error.status in (401, 403):
        return "Portdan 拒绝了认证或当前分组未授权图片请求"
    if error.status == 429:
        return "Portdan 当前限流，请稍后再试"
    if error.status == 404:
        return "Portdan 返回 404，图片请求未完成"
    if error.status == 0 or error.status >= 500:
        return "Portdan 请求失败；请求可能已经到达后台，请先检查 Portdan 记录后再决定是否重试"
    return "Portdan 拒绝了图片请求"


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if not args.prompt_stdin or args.timeout <= 0:
        print("图片提示词或超时时间无效", file=sys.stderr)
        return 2
    try:
        started = time.monotonic()
        prompt = _prompt_from_stdin()
        request_config = resolve_request_config()
        body = _payload(request_config, prompt, args.size, args.quality)
        print(
            "正在通过 Portdan 调用 OpenAI gpt-image-2（{}）生成图片…".format(
                QUALITY_LABELS[args.quality]
            ),
            file=sys.stderr,
            flush=True,
        )
        raw = _post(request_config.api_key, body, min(args.timeout, 900.0))
        image = _image_bytes(raw)
        output = _save_png(image).absolute()
        print(output)
        print(
            "已通过 Portdan 调用 OpenAI gpt-image-2 生成，耗时 {:.1f} 秒".format(
                time.monotonic() - started
            ),
            file=sys.stderr,
        )
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
