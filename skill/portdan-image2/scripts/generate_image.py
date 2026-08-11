#!/usr/bin/env python3
"""Thin GPT Images adapter for Portdan's Images generations endpoint."""

from __future__ import annotations

import argparse
import base64
import binascii
import errno
import http.client
import json
import math
import os
import queue
import re
import secrets
import shutil
import socket
import sqlite3
import ssl
import stat
import sys
import tempfile
import threading
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # Python 3.9/3.10
    tomllib = None  # type: ignore


HOST = "portdan.com"
ENDPOINT = "https://portdan.com/v1/images/generations"
DEFAULT_RESPONSE_MODEL = "gpt-5.4-mini"
MISSING_KEY_MESSAGE = (
    "未读取到可用于 Portdan 的 API Key；本次没有发送图片请求。"
    "请检查当前 CC Switch Codex provider、设置 PORTDAN_API_KEY，或为本次请求提供 Key"
)
PROVIDED_KEY_MESSAGE = "提供的 API Key 无效；本次没有发送图片请求"
KEY_SOURCE_CC_SWITCH = "cc_switch_current_provider"
KEY_SOURCE_INSTALLED = "installed_codex_config"
KEY_SOURCE_CODEX_HOME = "codex_home"
KEY_SOURCE_CC_SWITCH_CONFIG = "cc_switch_config_dir"
KEY_SOURCE_DEFAULT = "default_codex_config"
KEY_SOURCE_ENV = "portdan_api_key_env"
KEY_SOURCE_STDIN = "provided_stdin"
AUTH_KEY_FIELDS = ("OPENAI_API_KEY", "CODEX_API_KEY", "API_KEY", "api_key", "apiKey")
PROVIDER_KEY_FIELDS = AUTH_KEY_FIELDS + ("experimental_bearer_token",)
PROVIDER_CONFIG_FIELDS = PROVIDER_KEY_FIELDS + (
    "base_url",
    "wire_api",
    "env_key",
    "requires_openai_auth",
)
KEY_SOURCE_LABELS = {
    KEY_SOURCE_CC_SWITCH: "CC Switch 当前 Codex provider",
    KEY_SOURCE_INSTALLED: "已安装 Skill 所在 Codex 配置",
    KEY_SOURCE_CODEX_HOME: "CODEX_HOME",
    KEY_SOURCE_CC_SWITCH_CONFIG: "CC Switch codexConfigDir",
    KEY_SOURCE_DEFAULT: "~/.codex",
    KEY_SOURCE_ENV: "PORTDAN_API_KEY 环境变量",
    KEY_SOURCE_STDIN: "本次提供的 Key",
    "unknown": "未知来源",
}
MAX_CONFIG_BYTES = 2 * 1024 * 1024
MAX_SETTINGS_BYTES = 512 * 1024
MAX_KEY_BYTES = 8192
MAX_REQUEST_BODY_BYTES = 256 * 1024 * 1024
DEFAULT_JSON_MEMORY_LIMIT_BYTES = 256 * 1024 * 1024
DISK_FREE_RESERVE_BYTES = 256 * 1024 * 1024
BASE64_DECODE_CHARS = 1024 * 1024
CC_SWITCH_DB_TIMEOUT_SECONDS = 0.2
REQUEST_ID_HEADERS = (
    "x-request-id",
    "request-id",
    "openai-request-id",
    "x-portdan-request-id",
)
MAX_REQUEST_ID_CHARS = 64
CONNECT_TIMEOUT_SECONDS = 15.0
IDLE_TIMEOUT_SECONDS = 1800.0
HEARTBEAT_INTERVAL_SECONDS = 20.0
READ_CHUNK_BYTES = 64 * 1024
PROXY_MODES = ("direct", "system")


class ConfigError(RuntimeError):
    pass


class ProvidedKeyError(ValueError):
    pass


class InputError(ValueError):
    def __init__(self, code: str = "invalid_input", stage: str = "input") -> None:
        self.code = code
        self.stage = stage
        super().__init__(code)


class LocalResourceError(RuntimeError):
    def __init__(self, code: str = "local_resource_limit", stage: str = "resource") -> None:
        self.code = code
        self.stage = stage
        super().__init__(code)


class RequestError(RuntimeError):
    def __init__(
        self,
        status: int = 0,
        *,
        kind: Optional[str] = None,
        response_started: bool = False,
        request_id: Optional[str] = None,
        stage: Optional[str] = None,
        elapsed: Optional[float] = None,
    ) -> None:
        self.status = status
        self.kind = kind or ("http" if status else "transport")
        self.response_started = response_started
        self.request_id = request_id
        self.stage = stage or ("stream" if response_started else "connect")
        self.elapsed = elapsed
        super().__init__(str(status))


class ResponseError(RuntimeError):
    def __init__(
        self,
        *,
        stage: str = "decode",
        request_id: Optional[str] = None,
        elapsed: Optional[float] = None,
    ) -> None:
        self.stage = stage
        self.request_id = request_id
        self.elapsed = elapsed
        super().__init__(stage)


class PartialImageError(RuntimeError):
    def __init__(
        self,
        completed: int,
        expected: Optional[int],
        *,
        request_id: Optional[str] = None,
        elapsed: Optional[float] = None,
    ) -> None:
        self.completed = completed
        self.expected = expected
        self.request_id = request_id
        self.elapsed = elapsed
        self.staged_batch: Optional[Any] = None
        super().__init__("received {} of {} completed images".format(completed, expected))


class OutputRecoveryError(RuntimeError):
    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(str(path))


class OutputWriteError(RuntimeError):
    pass


@dataclass(frozen=True)
class RequestConfig:
    api_key: str = field(repr=False)
    model: str = DEFAULT_RESPONSE_MODEL
    source: str = "unknown"


@dataclass(frozen=True)
class PostResult:
    body: bytes
    request_id: str
    first_event_seconds: Optional[float] = None
    image_count: int = 0


@dataclass(frozen=True)
class EncodedImage:
    value: str = field(repr=False)
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Artifact:
    path: Path
    format: str
    bytes: int


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
        dotted = re.fullmatch(
            r'model_providers\.(?:"([^"\\]+)"|\'([^\'\\]+)\'|([A-Za-z0-9_-]+))\.'
            r"([A-Za-z0-9_-]+)",
            key,
        )
        if dotted:
            provider_name = next(value for value in dotted.groups()[:3] if value is not None)
            field_name = dotted.group(4)
            if field_name in PROVIDER_CONFIG_FIELDS:
                providers.setdefault(provider_name, {})[field_name] = _toml_value(raw)
            continue
        if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            continue
        value = _toml_value(raw)
        if current is None:
            if key in ("model", "model_provider", "openai_base_url"):
                top[key] = value
        elif key in PROVIDER_CONFIG_FIELDS:
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
    if not isinstance(base_url, str) or base_url != base_url.strip():
        return False
    try:
        parsed = urlsplit(base_url)
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
        or parsed.path
        not in (
            "",
            "/",
            "/v1",
            "/v1/",
            "/v1/responses",
            "/v1/responses/",
            "/v1/images/generations",
            "/v1/images/generations/",
            "/backend-api/codex",
            "/backend-api/codex/",
        )
    )


def _key(value: Any) -> str:
    if not isinstance(value, str):
        raise ConfigError()
    result = value.strip()
    if result.lower().startswith("bearer "):
        result = result[7:].strip()
    try:
        encoded_length = len(result.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ConfigError() from exc
    if (
        not result
        or encoded_length > MAX_KEY_BYTES
        or any(ord(c) < 32 or c.isspace() for c in result)
    ):
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
    for field_name in PROVIDER_KEY_FIELDS:
        key = _maybe_key(payload.get(field_name))
        if key:
            return key
    return None


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


def _provider_is_portdan(provider: Dict[str, Any]) -> bool:
    return _is_portdan_url(provider.get("base_url"))


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
    return active_provider is None or _provider_is_portdan(active_provider[1])


def _config_auth_allowed(config: Dict[str, Any], providers: list[tuple[str, Dict[str, Any]]]) -> bool:
    if _top_level_portdan_applies(config, providers):
        return True
    active = config.get("model_provider")
    if isinstance(active, str):
        active_provider = next(
            ((name, provider) for name, provider in providers if name == active), None
        )
        if active_provider is not None and _provider_is_portdan(active_provider[1]):
            return True
    return len(providers) == 1 and _provider_is_portdan(providers[0][1])


def _key_from_provider(
    provider: Dict[str, Any], environ: Mapping[str, str]
) -> Optional[str]:
    for field_name in PROVIDER_KEY_FIELDS:
        inline = _maybe_key(provider.get(field_name))
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
    config: Dict[str, Any],
    auth: Any,
    environ: Mapping[str, str],
    source: str = KEY_SOURCE_DEFAULT,
) -> Optional[RequestConfig]:
    providers = _configured_providers(config)
    model = _configured_model(config)
    active = config.get("model_provider")
    active_provider = next(
        (
            (name, provider)
            for name, provider in providers
            if isinstance(active, str) and name == active
        ),
        None,
    )
    if active_provider is not None:
        _, provider = active_provider
        if not _provider_is_portdan(provider):
            return None
        direct = _key_from_provider(provider, environ)
        if direct:
            return RequestConfig(api_key=direct, model=model, source=source)
        auth_key = _auth_key(auth)
        if auth_key:
            return RequestConfig(api_key=auth_key, model=model, source=source)
        return None

    top_level_portdan = _top_level_portdan_applies(config, providers)
    if top_level_portdan:
        auth_key = _auth_key(auth)
        if auth_key:
            return RequestConfig(api_key=auth_key, model=model, source=source)

    if isinstance(active, str):
        return None

    candidates: set[str] = set()
    portdan_providers = [
        (name, provider)
        for name, provider in providers
        if _provider_is_portdan(provider)
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
        return RequestConfig(api_key=next(iter(candidates)), model=selected_model, source=source)
    if len(providers) == 1 and len(portdan_providers) == 1:
        auth_key = _auth_key(auth)
        if auth_key:
            return RequestConfig(api_key=auth_key, model=model, source=source)
    return None


def _read_auth(path: Path) -> Optional[Dict[str, Any]]:
    try:
        raw = _read_small_file(path, MAX_SETTINGS_BYTES)
        return _json_object(raw.decode("utf-8-sig"))
    except (ConfigError, UnicodeDecodeError):
        return None


def _request_config_from_config_root(
    root: Path,
    environ: Mapping[str, str],
    source: str = KEY_SOURCE_DEFAULT,
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
            and _provider_is_portdan(provider)
        ),
        None,
    )
    if active_provider is not None:
        direct = _key_from_provider(active_provider, environ)
        if direct:
            return RequestConfig(api_key=direct, model=model, source=source)
    elif not _top_level_portdan_applies(config, providers) and len(providers) == 1:
        name, provider = providers[0]
        if _provider_is_portdan(provider):
            direct = _key_from_provider(provider, environ)
            if direct:
                return RequestConfig(api_key=direct, model=model, source=source)
    auth = _read_auth(root / "auth.json") if _config_auth_allowed(config, providers) else None
    return _request_config_from_config(config, auth, environ, source)


def _key_from_config_root(root: Path, environ: Mapping[str, str]) -> Optional[str]:
    request_config = _request_config_from_config_root(root, environ)
    return request_config.api_key if request_config is not None else None


def _cc_switch_settings(home: Path) -> Optional[Dict[str, Any]]:
    try:
        raw = _read_small_file(home / ".cc-switch" / "settings.json", MAX_SETTINGS_BYTES)
        return _json_object(raw.decode("utf-8-sig"))
    except (ConfigError, UnicodeDecodeError):
        return None


def _cc_switch_current_provider_id(home: Path) -> Optional[str]:
    settings = _cc_switch_settings(home)
    value = settings.get("currentProviderCodex") if settings else None
    if not isinstance(value, str):
        return None
    provider_id = value
    if (
        not provider_id
        or provider_id != provider_id.strip()
        or len(provider_id) > 512
        or any(ord(character) < 32 for character in provider_id)
    ):
        return None
    return provider_id


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
        if not {"settings_config", "app_type"}.issubset(columns):
            return None
        identity_columns = [
            name for name in ("website_url", "provider_type") if name in columns
        ]
        selected = ", ".join(["settings_config"] + identity_columns)
        rows = []
        current_provider_id = _cc_switch_current_provider_id(home)
        if current_provider_id is not None and "id" in columns:
            rows = connection.execute(
                "SELECT " + selected + " FROM providers "
                "WHERE app_type = 'codex' AND id = ? ORDER BY rowid DESC LIMIT 1",
                (current_provider_id,),
            ).fetchall()
        if not rows and "is_current" in columns:
            rows = connection.execute(
                "SELECT " + selected + " FROM providers "
                "WHERE app_type = 'codex' AND is_current = 1 "
                "ORDER BY rowid DESC LIMIT 1"
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
        identity_is_portdan = _is_portdan_url(identity.get("website_url"))
        config_is_portdan = config is not None and _config_auth_allowed(
            config, _configured_providers(config)
        )
        if identity_is_portdan or config_is_portdan:
            key = _auth_key(settings.get("auth"))
            if key:
                return RequestConfig(
                    api_key=key,
                    model=model,
                    source=KEY_SOURCE_CC_SWITCH,
                )
        if config is not None:
            request_config = _request_config_from_config(
                config,
                None,
                environ,
                KEY_SOURCE_CC_SWITCH,
            )
            if request_config is not None:
                return request_config
    return None


def _key_from_cc_switch_database(home: Path, environ: Mapping[str, str]) -> Optional[str]:
    request_config = _request_config_from_cc_switch_database(home, environ)
    return request_config.api_key if request_config is not None else None


def _custom_codex_root(home: Path) -> Optional[Path]:
    settings = _cc_switch_settings(home)
    custom = settings.get("codexConfigDir") if settings else None
    if not isinstance(custom, str) or not custom.strip():
        return None
    return _existing_directory(Path(custom.strip()).expanduser())


def _candidate_config_roots(
    home: Path, environ: Mapping[str, str]
) -> Iterator[tuple[Path, str]]:
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
            yield installed, KEY_SOURCE_INSTALLED

    codex_home = environ.get("CODEX_HOME")
    if codex_home:
        configured = resolve(Path(codex_home).expanduser())
        if configured is not None:
            yield configured, KEY_SOURCE_CODEX_HOME

    custom = _custom_codex_root(home)
    if custom is not None and str(custom) not in seen:
        seen.add(str(custom))
        yield custom, KEY_SOURCE_CC_SWITCH_CONFIG

    default = resolve(home / ".codex")
    if default is not None:
        yield default, KEY_SOURCE_DEFAULT


def resolve_request_config() -> RequestConfig:
    home = Path.home()
    environ = os.environ
    request_config = _request_config_from_cc_switch_database(home, environ)
    if request_config is not None:
        return request_config
    for root, source in _candidate_config_roots(home, environ):
        request_config = _request_config_from_config_root(root, environ, source)
        if request_config is not None:
            return request_config
    key = _maybe_key(environ.get("PORTDAN_API_KEY"))
    if key:
        return RequestConfig(api_key=key, source=KEY_SOURCE_ENV)
    raise ConfigError()


def resolve_api_key() -> str:
    return resolve_request_config().api_key


def _usable_positive_integer(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _validate_image_model(value: Any) -> None:
    if not isinstance(value, str) or not re.fullmatch(
        r"gpt-image-[A-Za-z0-9][A-Za-z0-9._-]*", value
    ):
        raise InputError("unsupported_model", "request")


def _serialize_request(body: Mapping[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise InputError("invalid_request_json", "request") from exc
    if len(encoded) > MAX_REQUEST_BODY_BYTES:
        raise LocalResourceError("request_body_too_large", "request")
    return encoded


def _payload(
    request_config: RequestConfig,
    prompt: str,
    size: Optional[str] = None,
    quality: Optional[str] = None,
    count: Optional[int] = None,
    model: Optional[str] = None,
) -> bytes:
    del request_config  # Key discovery is deliberately independent of the Images body.
    body: Dict[str, Any] = {"prompt": prompt}
    if model is not None:
        _validate_image_model(model)
        body["model"] = model
    if size is not None:
        body["size"] = size
    if quality is not None:
        body["quality"] = quality
    if count is not None:
        body["n"] = count
    body["stream"] = True
    body["response_format"] = "b64_json"
    return _serialize_request(body)


def _raw_payload(value: Any) -> tuple[bytes, Optional[int]]:
    if not isinstance(value, dict):
        raise InputError("request_json_not_object", "input")
    body = dict(value)
    if "model" in body:
        _validate_image_model(body["model"])
    body["stream"] = True
    body["response_format"] = "b64_json"
    return _serialize_request(body), _usable_positive_integer(body.get("n"))


def _request_id(headers: Any) -> Optional[str]:
    if headers is None:
        return None
    for header_name in REQUEST_ID_HEADERS:
        try:
            value = headers.get(header_name)
        except (AttributeError, TypeError, ValueError):
            continue
        if not isinstance(value, str):
            continue
        request_id = value.strip()
        if (
            request_id
            and len(request_id) <= MAX_REQUEST_ID_CHARS
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", request_id)
        ):
            return request_id
    return None


def _transport_error(
    error: BaseException,
    *,
    response_started: bool,
    status: int = 0,
    request_id: Optional[str] = None,
    stage: Optional[str] = None,
    elapsed: Optional[float] = None,
) -> RequestError:
    reason: Any = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, (TimeoutError, socket.timeout)) or (
        isinstance(reason, OSError) and reason.errno == errno.ETIMEDOUT
    ):
        kind = "idle_timeout" if response_started else "timeout"
    elif isinstance(reason, ssl.SSLError):
        kind = "connect" if response_started else "tls"
    elif isinstance(reason, socket.gaierror):
        kind = "dns"
    else:
        kind = "connect"
    return RequestError(
        status,
        kind=kind,
        response_started=response_started,
        request_id=request_id,
        stage=stage,
        elapsed=elapsed,
    )


def _new_request_id() -> str:
    return "pdi-" + secrets.token_hex(16)


def _set_response_socket_timeout(response: Any, timeout: float) -> None:
    """Best-effort read timeout update for urllib's wrapped HTTP socket."""
    first = getattr(response, "fp", None)
    second = getattr(first, "fp", None)
    candidates = (
        getattr(getattr(first, "raw", None), "_sock", None),
        getattr(getattr(second, "raw", None), "_sock", None),
        getattr(first, "_sock", None),
        getattr(second, "_sock", None),
        getattr(response, "_sock", None),
    )
    for candidate in candidates:
        if candidate is not None and callable(getattr(candidate, "settimeout", None)):
            candidate.settimeout(timeout)
            return


def _is_timeout_error(error: BaseException) -> bool:
    reason: Any = error.reason if isinstance(error, URLError) else error
    return isinstance(reason, (TimeoutError, socket.timeout)) or (
        isinstance(reason, OSError) and reason.errno == errno.ETIMEDOUT
    )


def _stream_read_timeout_error(
    *,
    started: float,
    overall_timeout: Optional[float],
    overall_driven: bool,
    request_id: str,
    now: Optional[float] = None,
) -> RequestError:
    elapsed = (time.monotonic() if now is None else now) - started
    return RequestError(
        200,
        kind=(
            "overall_timeout"
            if overall_timeout is not None
            and (overall_driven or elapsed >= overall_timeout)
            else "idle_timeout"
        ),
        response_started=True,
        request_id=request_id,
        stage="stream",
        elapsed=elapsed,
    )


def _close_response(response: Any) -> None:
    try:
        response.close()
    except (AttributeError, OSError, ValueError):
        pass


def _close_response_async(response: Any) -> None:
    threading.Thread(
        target=_close_response,
        args=(response,),
        daemon=True,
    ).start()


def _open_response_once(
    opener: Any,
    request: Request,
    *,
    started: float,
    overall_timeout: Optional[float],
    connect_timeout: float,
    request_id: str,
) -> Any:
    outcomes: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
    expired = threading.Event()

    def open_worker() -> None:
        try:
            response = opener.open(
                request,
                timeout=(
                    connect_timeout
                    if overall_timeout is None
                    else min(connect_timeout, overall_timeout)
                ),
            )
        except BaseException as exc:
            try:
                outcomes.put_nowait(("error", exc))
            except queue.Full:
                pass
            return
        if expired.is_set():
            _close_response(response)
            return
        try:
            outcomes.put_nowait(("response", response))
        except queue.Full:
            _close_response(response)
            return
        if expired.is_set():
            _close_response(response)

    threading.Thread(target=open_worker, daemon=True).start()
    wait_seconds = (
        connect_timeout
        if overall_timeout is None
        else min(connect_timeout, overall_timeout)
    )
    try:
        kind, value = outcomes.get(timeout=wait_seconds)
    except queue.Empty:
        expired.set()
        try:
            late_kind, late_value = outcomes.get_nowait()
        except queue.Empty:
            pass
        else:
            if late_kind == "response":
                _close_response_async(late_value)
        elapsed = time.monotonic() - started
        raise RequestError(
            0,
            kind=(
                "overall_timeout"
                if overall_timeout is not None and overall_timeout <= connect_timeout
                else "timeout"
            ),
            response_started=False,
            request_id=request_id,
            stage="connect",
            elapsed=elapsed,
        ) from None
    elapsed = time.monotonic() - started
    if elapsed >= wait_seconds:
        expired.set()
        if kind == "response":
            _close_response_async(value)
        raise RequestError(
            0,
            kind=(
                "overall_timeout"
                if overall_timeout is not None and overall_timeout <= connect_timeout
                else "timeout"
            ),
            response_started=False,
            request_id=request_id,
            stage="connect",
            elapsed=elapsed,
        )
    if kind == "error":
        raise value
    return value


class _DeadlineResponseReader:
    """Incremental response reader with hard idle and overall deadlines."""

    def __init__(
        self,
        response: Any,
        *,
        started: float,
        overall_timeout: Optional[float],
        idle_timeout: float,
        request_id: str,
    ) -> None:
        self.response = response
        self.started = started
        self.overall_timeout = overall_timeout
        self.idle_timeout = idle_timeout
        self.request_id = request_id
        self.buffer = bytearray()
        self.eof = False
        self.last_data_at = time.monotonic()
        self.outcomes: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=2)
        self.stop_event = threading.Event()
        self.reader = self._resolve_reader()
        socket_timeout = idle_timeout
        if overall_timeout is not None:
            socket_timeout = min(
                socket_timeout,
                max(0.001, overall_timeout - (self.last_data_at - started)),
            )
        _set_response_socket_timeout(response, socket_timeout)
        self.worker = threading.Thread(target=self._read_worker, daemon=True)
        self.worker.start()

    def _resolve_reader(self) -> Any:
        first = getattr(self.response, "fp", None)
        second = getattr(first, "fp", None)
        for candidate in (self.response, first, second):
            read1 = getattr(candidate, "read1", None)
            if callable(read1):
                return read1
        return self.response.read

    def _put(self, item: tuple[str, Any]) -> None:
        while not self.stop_event.is_set():
            try:
                self.outcomes.put(item, timeout=0.05)
                return
            except queue.Full:
                continue

    def _read_worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                chunk = self.reader(READ_CHUNK_BYTES)
            except BaseException as exc:
                self._put(("error", exc))
                return
            if not chunk:
                self._put(("eof", None))
                return
            self._put(("data", bytes(chunk)))

    def _deadline_error(self, *, now: float, overall: bool) -> RequestError:
        return RequestError(
            200,
            kind="overall_timeout" if overall else "idle_timeout",
            response_started=True,
            request_id=self.request_id,
            stage="stream",
            elapsed=now - self.started,
        )

    def _abort(self) -> None:
        self.stop_event.set()
        _close_response_async(self.response)

    def _next_chunk(self) -> bytes:
        while True:
            now = time.monotonic()
            overall_remaining = (
                None
                if self.overall_timeout is None
                else self.overall_timeout - (now - self.started)
            )
            idle_remaining = self.idle_timeout - (now - self.last_data_at)
            if overall_remaining is not None and overall_remaining <= 0:
                self._abort()
                raise self._deadline_error(now=now, overall=True)
            if idle_remaining <= 0:
                self._abort()
                raise self._deadline_error(now=now, overall=False)
            try:
                wait_seconds = idle_remaining
                if overall_remaining is not None:
                    wait_seconds = min(wait_seconds, overall_remaining)
                kind, value = self.outcomes.get(timeout=wait_seconds)
            except queue.Empty:
                now = time.monotonic()
                overall = (
                    self.overall_timeout is not None
                    and now - self.started >= self.overall_timeout
                )
                self._abort()
                raise self._deadline_error(now=now, overall=overall) from None
            now = time.monotonic()
            if (
                self.overall_timeout is not None
                and now - self.started >= self.overall_timeout
            ):
                self._abort()
                raise self._deadline_error(now=now, overall=True)
            if kind == "error":
                if _is_timeout_error(value):
                    overall = (
                        self.overall_timeout is not None
                        and now - self.started >= self.overall_timeout
                    )
                    self._abort()
                    raise self._deadline_error(now=now, overall=overall) from None
                raise value
            if kind == "eof":
                self.eof = True
                return b""
            self.last_data_at = now
            socket_timeout = self.idle_timeout
            if self.overall_timeout is not None:
                socket_timeout = min(
                    socket_timeout,
                    max(0.001, self.overall_timeout - (now - self.started)),
                )
            _set_response_socket_timeout(self.response, socket_timeout)
            return value

    def readline(self, limit: int = -1) -> bytes:
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                end = newline + 1
                if limit >= 0:
                    end = min(end, limit)
                result = bytes(self.buffer[:end])
                del self.buffer[:end]
                return result
            if limit >= 0 and len(self.buffer) >= limit:
                result = bytes(self.buffer[:limit])
                del self.buffer[:limit]
                return result
            if self.eof:
                result = bytes(self.buffer)
                self.buffer.clear()
                return result
            self.buffer.extend(self._next_chunk())

    def read(self, limit: int = -1) -> bytes:
        while not self.eof and (limit < 0 or len(self.buffer) < limit):
            self.buffer.extend(self._next_chunk())
        if limit < 0:
            result = bytes(self.buffer)
            self.buffer.clear()
            return result
        result = bytes(self.buffer[:limit])
        del self.buffer[:limit]
        return result

    def close(self) -> None:
        self._abort()


class _Heartbeat:
    def __init__(self, started: float, request_id: str, interval: float) -> None:
        self.started = started
        self.request_id = request_id
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def __enter__(self) -> "_Heartbeat":
        if self.interval > 0:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            print(
                "仍在等待 Portdan 图片流… 已等待 {:.0f} 秒；请求 ID：{}".format(
                    time.monotonic() - self.started,
                    self.request_id,
                ),
                file=sys.stderr,
                flush=True,
            )

    def __exit__(self, *_args: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)


def _read_bounded_json_response(
    response: Any,
    *,
    started: float,
    overall_timeout: Optional[float],
    idle_timeout: float,
    request_id: str,
    max_response_bytes: int = DEFAULT_JSON_MEMORY_LIMIT_BYTES,
) -> bytes:
    elapsed = time.monotonic() - started
    remaining = None if overall_timeout is None else overall_timeout - elapsed
    if remaining is not None and remaining <= 0:
        raise _stream_read_timeout_error(
            started=started,
            overall_timeout=overall_timeout,
            overall_driven=True,
            request_id=request_id,
        )
    overall_driven = remaining is not None and remaining <= idle_timeout
    _set_response_socket_timeout(
        response,
        idle_timeout if remaining is None else min(idle_timeout, remaining),
    )
    try:
        data = response.read(max_response_bytes + 1)
    except (OSError, URLError, TimeoutError, http.client.HTTPException) as exc:
        if _is_timeout_error(exc):
            raise _stream_read_timeout_error(
                started=started,
                overall_timeout=overall_timeout,
                overall_driven=overall_driven,
                request_id=request_id,
            ) from None
        raise
    if len(data) > max_response_bytes:
        raise LocalResourceError("json_memory_limit", "stream")
    elapsed_after_read = time.monotonic() - started
    if overall_timeout is not None and elapsed_after_read > overall_timeout:
        raise RequestError(
            200,
            kind="overall_timeout",
            response_started=True,
            request_id=request_id,
            stage="stream",
            elapsed=elapsed_after_read,
        )
    return data


class _InvalidResponseJSON(ValueError):
    pass


def _response_object_from_pairs(pairs: list[tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _InvalidResponseJSON("duplicate response field")
        value[key] = item
    return value


def _reject_response_constant(_value: str) -> Any:
    raise _InvalidResponseJSON("non-standard JSON number")


def _strict_response_json(
    source: Any,
    *,
    stage: str,
    request_id: Optional[str] = None,
    elapsed: Optional[float] = None,
) -> Any:
    """Decode provider JSON without last-wins fields or non-finite numbers."""
    options = {
        "object_pairs_hook": _response_object_from_pairs,
        "parse_constant": _reject_response_constant,
    }
    try:
        if hasattr(source, "read"):
            return json.load(source, **options)
        return json.loads(source, **options)
    except (UnicodeDecodeError, TypeError, ValueError) as exc:
        raise ResponseError(
            stage=stage,
            request_id=request_id,
            elapsed=elapsed,
        ) from exc


def _consume_json_response_via_temp(
    response: Any,
    *,
    started: float,
    request_id: str,
    expected_count: Optional[int],
    on_image: Callable[[Any], None],
    json_memory_limit: int,
    prefix: bytes = b"",
) -> int:
    """Spool network chunks privately before stdlib JSON decoding."""
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix="portdan-image2-response-", suffix=".json"
        )
    except OSError as exc:
        raise OutputWriteError() from exc
    path = Path(raw_path)
    identity: Optional[tuple[int, int]] = None
    total = 0
    try:
        try:
            os.fchmod(descriptor, 0o600)
            info = os.fstat(descriptor)
        except OSError as exc:
            raise OutputWriteError() from exc
        identity = (info.st_dev, info.st_ino)
        if not stat.S_ISREG(info.st_mode):
            raise OutputWriteError()
        if prefix:
            if len(prefix) > json_memory_limit:
                raise LocalResourceError("json_memory_limit", "stream")
            try:
                usage = shutil.disk_usage(str(path.parent))
            except OSError as exc:
                raise OutputWriteError() from exc
            if usage.free - len(prefix) < DISK_FREE_RESERVE_BYTES:
                raise LocalResourceError("insufficient_disk_space", "stream")
            try:
                _write_all(descriptor, prefix)
            except OSError as exc:
                raise OutputWriteError() from exc
            total = len(prefix)
        while True:
            remaining = json_memory_limit - total
            if remaining < 0:
                raise LocalResourceError("json_memory_limit", "stream")
            chunk = response.read(min(READ_CHUNK_BYTES, remaining + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > json_memory_limit:
                raise LocalResourceError("json_memory_limit", "stream")
            try:
                usage = shutil.disk_usage(str(path.parent))
            except OSError as exc:
                raise OutputWriteError() from exc
            if usage.free - len(chunk) < DISK_FREE_RESERVE_BYTES:
                raise LocalResourceError("insufficient_disk_space", "stream")
            try:
                _write_all(descriptor, chunk)
            except OSError as exc:
                raise OutputWriteError() from exc
        try:
            os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
        except OSError as exc:
            raise OutputWriteError() from exc
        try:
            with os.fdopen(os.dup(descriptor), "r", encoding="utf-8") as handle:
                value = _strict_response_json(
                    handle,
                    stage="response_parse",
                    request_id=request_id,
                )
        except OSError as exc:
            raise OutputWriteError() from exc
        return _consume_image_value(
            value,
            expected_count=expected_count,
            on_image=on_image,
            request_id=request_id,
            elapsed=time.monotonic() - started,
        )
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
        if identity is not None:
            try:
                _unlink_owned(path, identity)
            except (FileNotFoundError, OSError):
                pass


def _read_image_stream(
    response: Any,
    *,
    started: float,
    overall_timeout: Optional[float],
    idle_timeout: float,
    request_id: str,
    expected_count: Optional[int] = None,
    on_image: Optional[Callable[[Any], None]] = None,
    json_memory_limit: int = DEFAULT_JSON_MEMORY_LIMIT_BYTES,
) -> tuple[bytes, Optional[float]]:
    """Consume Images SSE in arrival order without a response-total size cap."""
    if expected_count is not None and _usable_positive_integer(expected_count) is None:
        raise ValueError()
    event_name = ""
    data_lines: list[bytes] = []
    event_bytes = 0
    completed = 0
    first_event_seconds: Optional[float] = None
    retained: list[bytes] = []
    first_nonempty = True

    def partial() -> PartialImageError:
        return PartialImageError(
            completed,
            expected_count,
            request_id=request_id,
            elapsed=time.monotonic() - started,
        )

    def normal_termination(kind: str = "truncated_stream") -> None:
        if expected_count is None:
            if completed:
                return
        elif completed == expected_count:
            return
        elif completed:
            raise partial()
        raise RequestError(
            200,
            kind=kind,
            response_started=True,
            request_id=request_id,
            stage="stream",
            elapsed=time.monotonic() - started,
        )

    def accept(value: Any, raw_payload: bytes) -> None:
        nonlocal completed
        if not isinstance(value, dict) or not isinstance(value.get("b64_json"), str):
            raise ResponseError(stage="stream_image", request_id=request_id)
        if expected_count is not None and completed >= expected_count:
            raise ResponseError(
                stage="stream_too_many_results",
                request_id=request_id,
                elapsed=time.monotonic() - started,
            )
        if on_image is None:
            retained.append(raw_payload)
        else:
            try:
                on_image(EncodedImage(value["b64_json"], value))
            except LocalResourceError:
                raise
            except OSError as exc:
                raise OutputWriteError() from exc
        completed += 1

    def finish_body() -> bytes:
        if on_image is not None:
            return b""
        if len(retained) == 1:
            return retained[0]
        return b'{"data":[' + b",".join(retained) + b"]}"

    def process_event() -> bool:
        nonlocal event_name, data_lines, event_bytes, first_event_seconds
        current_event = event_name
        event_name = ""
        if current_event in ("error", "response.failed"):
            data_lines = []
            event_bytes = 0
            if completed:
                raise partial()
            raise RequestError(
                200,
                kind="stream_error",
                response_started=True,
                request_id=request_id,
                stage="stream",
                elapsed=time.monotonic() - started,
            )
        if not data_lines:
            event_bytes = 0
            return False
        payload = b"\n".join(data_lines).strip()
        data_lines = []
        event_bytes = 0
        if first_event_seconds is None:
            first_event_seconds = time.monotonic() - started
        if payload == b"[DONE]":
            if expected_count is None:
                if completed:
                    return True
                normal_termination()
            if completed == expected_count:
                return True
            normal_termination()
        value = _strict_response_json(
            payload,
            stage="stream_parse",
            request_id=request_id,
        )
        payload_type = value.get("type") if isinstance(value, dict) else None
        if payload_type in ("error", "response.failed"):
            if completed:
                raise partial()
            raise RequestError(
                200,
                kind="stream_error",
                response_started=True,
                request_id=request_id,
                stage="stream",
                elapsed=time.monotonic() - started,
            )
        if current_event == "image_generation.completed" or payload_type == "image_generation.completed":
            accept(value, payload)
        return False

    while True:
        if overall_timeout is not None and time.monotonic() - started >= overall_timeout:
            if completed:
                raise partial()
            raise RequestError(
                200,
                kind="overall_timeout",
                response_started=True,
                request_id=request_id,
                stage="stream",
                elapsed=time.monotonic() - started,
            )
        try:
            line = response.readline(json_memory_limit + 1)
        except RequestError:
            if completed:
                raise partial() from None
            raise
        except (OSError, URLError, TimeoutError, http.client.HTTPException) as exc:
            if completed:
                raise partial() from None
            if _is_timeout_error(exc):
                now = time.monotonic()
                raise _stream_read_timeout_error(
                    started=started,
                    overall_timeout=overall_timeout,
                    overall_driven=(
                        overall_timeout is not None
                        and overall_timeout - (now - started) <= idle_timeout
                    ),
                    request_id=request_id,
                    now=now,
                ) from None
            raise
        if not line:
            if process_event():
                return finish_body(), first_event_seconds
            normal_termination()
            return finish_body(), first_event_seconds
        if len(line) > json_memory_limit:
            raise LocalResourceError("json_memory_limit", "stream")
        stripped = line.strip()
        if first_nonempty and stripped:
            first_nonempty = False
            if stripped.startswith(b"{"):
                if on_image is not None:
                    _consume_json_response_via_temp(
                        response,
                        started=started,
                        request_id=request_id,
                        expected_count=expected_count,
                        on_image=on_image,
                        json_memory_limit=json_memory_limit,
                        prefix=line,
                    )
                    return b"", time.monotonic() - started
                rest = response.read(max(0, json_memory_limit - len(line)) + 1)
                raw = line + rest
                if len(raw) > json_memory_limit:
                    raise LocalResourceError("json_memory_limit", "stream")
                elapsed_after_read = time.monotonic() - started
                if overall_timeout is not None and elapsed_after_read > overall_timeout:
                    raise RequestError(
                        200,
                        kind="overall_timeout",
                        response_started=True,
                        request_id=request_id,
                        stage="stream",
                        elapsed=elapsed_after_read,
                    )
                _consume_image_response(
                    raw,
                    expected_count=expected_count,
                    on_image=on_image,
                    request_id=request_id,
                    elapsed=elapsed_after_read,
                )
                return b"" if on_image is not None else raw, elapsed_after_read
        if not stripped:
            if process_event():
                return finish_body(), first_event_seconds
            continue
        if stripped.startswith(b":"):
            continue
        if stripped.startswith(b"event:"):
            event_name = stripped[6:].strip().decode("utf-8", "replace")
            continue
        if stripped.startswith(b"data:"):
            data_value = stripped[5:].lstrip()
            projected = event_bytes + len(data_value) + (1 if data_lines else 0)
            if projected > json_memory_limit:
                raise LocalResourceError("json_memory_limit", "stream")
            data_lines.append(data_value)
            event_bytes = projected
            continue
        raise ResponseError(stage="stream_parse", request_id=request_id)


def _post(
    api_key: str,
    body: bytes,
    timeout: Optional[float] = None,
    *,
    proxy_mode: str = "direct",
    connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
    idle_timeout: float = IDLE_TIMEOUT_SECONDS,
    heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS,
    client_request_id: Optional[str] = None,
    expected_count: Optional[int] = None,
    on_image: Optional[Callable[[Any], None]] = None,
    json_memory_limit: int = DEFAULT_JSON_MEMORY_LIMIT_BYTES,
) -> PostResult:
    if proxy_mode not in PROXY_MODES:
        raise ValueError()
    if expected_count is not None and _usable_positive_integer(expected_count) is None:
        raise ValueError()
    if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
        raise ValueError()
    if not math.isfinite(connect_timeout) or connect_timeout <= 0:
        raise ValueError()
    if not math.isfinite(idle_timeout) or idle_timeout <= 0:
        raise ValueError()
    if json_memory_limit <= 0:
        raise ValueError()
    if len(body) > MAX_REQUEST_BODY_BYTES:
        raise LocalResourceError("request_body_too_large", "request")
    started = time.monotonic()
    generated_request_id = client_request_id or _new_request_id()
    request = Request(
        ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + api_key,
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": "portdan-image2-runner/5.0",
            "X-Request-ID": generated_request_id,
        },
    )
    proxy_handler = ProxyHandler({}) if proxy_mode == "direct" else ProxyHandler()
    opener = build_opener(proxy_handler, _NoRedirect())
    response_started = False
    status = 0
    request_id: Optional[str] = None
    completed_count = 0

    def counted_image(payload: Any) -> None:
        nonlocal completed_count
        if on_image is not None:
            on_image(payload)
        completed_count += 1

    try:
        with _Heartbeat(started, generated_request_id, heartbeat_interval):
            response = _open_response_once(
                opener,
                request,
                started=started,
                overall_timeout=timeout,
                connect_timeout=connect_timeout,
                request_id=generated_request_id,
            )
            try:
                response_started = True
                status = int(getattr(response, "status", response.getcode()))
                request_id = _request_id(getattr(response, "headers", None)) or generated_request_id
                if not 200 <= status < 300:
                    raise RequestError(
                        status,
                        kind="http",
                        response_started=True,
                        request_id=request_id,
                        stage="headers",
                        elapsed=time.monotonic() - started,
                    )
                content_type = ""
                try:
                    content_type = response.headers.get("Content-Type", "")
                except (AttributeError, TypeError, ValueError):
                    pass
                reader = _DeadlineResponseReader(
                    response,
                    started=started,
                    overall_timeout=timeout,
                    idle_timeout=idle_timeout,
                    request_id=request_id,
                )
                try:
                    if "text/event-stream" in content_type.lower():
                        data, first_event_seconds = _read_image_stream(
                            reader,
                            started=started,
                            overall_timeout=timeout,
                            idle_timeout=idle_timeout,
                            request_id=request_id,
                            expected_count=expected_count,
                            on_image=counted_image if on_image is not None else None,
                            json_memory_limit=json_memory_limit,
                        )
                    else:
                        if on_image is not None:
                            completed_count = _consume_json_response_via_temp(
                                reader,
                                started=started,
                                request_id=request_id,
                                expected_count=expected_count,
                                on_image=counted_image,
                                json_memory_limit=json_memory_limit,
                            )
                            data = b""
                        else:
                            data = _read_bounded_json_response(
                                reader,
                                started=started,
                                overall_timeout=timeout,
                                idle_timeout=idle_timeout,
                                request_id=request_id,
                                max_response_bytes=json_memory_limit,
                            )
                        first_event_seconds = time.monotonic() - started
                finally:
                    reader.close()
            finally:
                _close_response_async(response)
    except HTTPError as exc:
        raise RequestError(
            int(exc.code),
            kind="http",
            response_started=True,
            request_id=_request_id(getattr(exc, "headers", None)) or generated_request_id,
            stage="headers",
            elapsed=time.monotonic() - started,
        ) from None
    except RequestError:
        raise
    except ResponseError as exc:
        if exc.request_id is None:
            exc.request_id = request_id or generated_request_id
        if exc.elapsed is None:
            exc.elapsed = time.monotonic() - started
        raise
    except (OSError, URLError, TimeoutError, http.client.HTTPException) as exc:
        raise _transport_error(
            exc,
            response_started=response_started,
            status=status,
            request_id=request_id or generated_request_id,
            stage="stream" if response_started else "connect",
            elapsed=time.monotonic() - started,
        ) from None
    return PostResult(
        body=data,
        request_id=request_id or generated_request_id,
        first_event_seconds=first_event_seconds,
        image_count=completed_count,
    )


def _image_items(raw: bytes) -> list[EncodedImage]:
    response = _strict_response_json(raw, stage="decode")
    return _image_items_from_value(response)


def _image_items_from_value(response: Any) -> list[EncodedImage]:
    values = list(_iter_image_items_from_value(response))
    if not values:
        raise ResponseError()
    return values


def _iter_image_items_from_value(response: Any) -> Iterator[EncodedImage]:
    found = False
    if isinstance(response, dict) and isinstance(response.get("b64_json"), str):
        found = True
        yield EncodedImage(response["b64_json"], response)
    elif isinstance(response, dict) and isinstance(response.get("data"), list):
        for item in response["data"]:
            if not isinstance(item, dict) or not isinstance(item.get("b64_json"), str):
                raise ResponseError(stage="response_image")
            found = True
            yield EncodedImage(item["b64_json"], item)
    elif isinstance(response, dict) and isinstance(response.get("output"), list):
        for item in response["output"]:
            if not isinstance(item, dict) or item.get("type") != "image_generation_call":
                continue
            if not isinstance(item.get("result"), str):
                raise ResponseError(stage="response_image")
            found = True
            yield EncodedImage(item["result"], item)
    if not found:
        raise ResponseError()


def _image_values(raw: bytes) -> list[str]:
    return [item.value for item in _image_items(raw)]


def _decode_image_value(value: str) -> bytes:
    if value.startswith("data:") and "," in value:
        value = value.split(",", 1)[1]
    try:
        data = base64.b64decode(re.sub(r"\s+", "", value), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ResponseError() from exc
    return data


def _image_bytes(raw: bytes) -> bytes:
    values = _image_values(raw)
    if len(values) != 1:
        raise ResponseError()
    return _decode_image_value(values[0])


def _consume_image_response(
    raw: bytes,
    *,
    expected_count: Optional[int],
    on_image: Optional[Callable[[Any], None]],
    request_id: Optional[str] = None,
    elapsed: Optional[float] = None,
) -> int:
    response = _strict_response_json(
        raw,
        stage="response_parse",
        request_id=request_id,
        elapsed=elapsed,
    )
    return _consume_image_value(
        response,
        expected_count=expected_count,
        on_image=on_image,
        request_id=request_id,
        elapsed=elapsed,
    )


def _consume_image_value(
    response: Any,
    *,
    expected_count: Optional[int],
    on_image: Optional[Callable[[Any], None]],
    request_id: Optional[str] = None,
    elapsed: Optional[float] = None,
) -> int:
    count = 0
    for value in _iter_image_items_from_value(response):
        if expected_count is not None and count >= expected_count:
            raise ResponseError(
                stage="response_too_many_results",
                request_id=request_id,
                elapsed=elapsed,
            )
        if on_image is not None:
            try:
                on_image(value)
            except LocalResourceError:
                raise
            except OSError as exc:
                raise OutputWriteError() from exc
        count += 1
    if expected_count is not None and count < expected_count:
        raise PartialImageError(
            count,
            expected_count,
            request_id=request_id,
            elapsed=elapsed,
        )
    return count


def _sanitize_png(data: bytes) -> bytes:
    """Legacy helper: validate the PNG signature without semantic image limits."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ResponseError()
    return data


def _format_from_bytes(prefix: bytes, metadata: Mapping[str, Any]) -> str:
    del metadata
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if prefix.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP":
        return "webp"
    return "bin"


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("could not write output")
        view = view[written:]


class _ImageBatch:
    def __init__(self, expected_count: Optional[int]) -> None:
        if expected_count is not None and _usable_positive_integer(expected_count) is None:
            raise ValueError()
        self.expected_count = expected_count
        self.entries: list[tuple[Path, tuple[int, int], int, str]] = []
        self.published = False
        self.preserve_recovery = False
        self._published_artifacts: list[Artifact] = []

        output = Path.cwd() / "portdan-images"
        output.mkdir(parents=True, exist_ok=True)
        info = output.lstat()
        if _is_link_like(info) or not stat.S_ISDIR(info.st_mode):
            raise OSError("output directory is invalid")
        self.directory = output.resolve(strict=True)
        directory_info = self.directory.lstat()
        if _is_link_like(directory_info):
            raise OSError("output directory changed unexpectedly")
        self.directory_identity = (directory_info.st_dev, directory_info.st_ino)
        self.staging: Optional[Path] = None
        self.staging_identity: Optional[tuple[int, int]] = None
        for _ in range(20):
            candidate = self.directory / (".portdan-image2-stage-" + secrets.token_hex(16))
            try:
                os.mkdir(str(candidate), 0o700)
            except FileExistsError:
                continue
            stage_info = candidate.lstat()
            if _is_link_like(stage_info) or not stat.S_ISDIR(stage_info.st_mode):
                raise OSError("staging directory is invalid")
            self.staging = candidate
            self.staging_identity = (stage_info.st_dev, stage_info.st_ino)
            break
        if self.staging is None:
            raise OSError("could not allocate a private staging directory")

    def __enter__(self) -> "_ImageBatch":
        return self

    def __exit__(self, _exc_type: Any, exc_value: Any, _traceback: Any) -> None:
        if (
            isinstance(exc_value, PartialImageError)
            and self.count == exc_value.completed
            and self.count > 0
            and (self.expected_count is None or self.count < self.expected_count)
        ):
            exc_value.staged_batch = self
            return
        if not self.published and not self.preserve_recovery:
            self._cleanup_staging()

    @property
    def count(self) -> int:
        return len(self.entries)

    @property
    def artifacts(self) -> list[Artifact]:
        return list(self._published_artifacts)

    def _check_slot(self) -> None:
        if self.expected_count is not None and self.count >= self.expected_count:
            raise ResponseError(stage="response_too_many_results")

    def _open_temporary(self) -> tuple[Path, int, tuple[int, int]]:
        assert self.staging is not None
        temporary = self.staging / "image-{}.part".format(self.count + 1)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(str(temporary), flags, 0o600)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            os.close(descriptor)
            raise OSError("staging output is not a regular file")
        return temporary, descriptor, (info.st_dev, info.st_ino)

    def _ensure_disk_space(self, incoming: int) -> None:
        usage = shutil.disk_usage(str(self.directory))
        if usage.free - incoming < DISK_FREE_RESERVE_BYTES:
            raise LocalResourceError("insufficient_disk_space", "save")

    def _commit_temporary(
        self,
        temporary: Path,
        identity: tuple[int, int],
        size: int,
        prefix: bytes,
        metadata: Mapping[str, Any],
    ) -> None:
        image_format = _format_from_bytes(prefix, metadata)
        final = temporary.with_name("image-{}.{}".format(self.count + 1, image_format))
        os.rename(str(temporary), str(final))
        current = final.lstat()
        if (
            not stat.S_ISREG(current.st_mode)
            or identity != (current.st_dev, current.st_ino)
            or current.st_size != size
        ):
            raise OSError("staging output changed unexpectedly")
        self.entries.append((final, identity, size, image_format))

    def stage_payload(self, payload: Any) -> None:
        self._check_slot()
        if isinstance(payload, EncodedImage):
            self._stage_base64(payload.value, payload.metadata)
            return
        if isinstance(payload, bytes):
            self._stage_bytes(payload, {})
            return
        raise ResponseError(stage="response_image")

    def stage_png(self, data: bytes) -> None:
        self._check_slot()
        self._stage_bytes(_sanitize_png(data), {"output_format": "png"})

    def _stage_bytes(self, data: bytes, metadata: Mapping[str, Any]) -> None:
        temporary, descriptor, identity = self._open_temporary()
        committed = False
        try:
            self._ensure_disk_space(len(data))
            _write_all(descriptor, data)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            self._commit_temporary(temporary, identity, len(data), data[:16], metadata)
            committed = True
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if not committed:
                try:
                    _unlink_owned(temporary, identity)
                except (FileNotFoundError, OSError):
                    pass

    def _stage_base64(self, value: str, metadata: Mapping[str, Any]) -> None:
        if value.startswith("data:"):
            comma = value.find(",")
            if comma < 0:
                raise ResponseError(stage="decode")
            value = value[comma + 1:]
        temporary, descriptor, identity = self._open_temporary()
        committed = False
        carry = ""
        saw_padding = False
        size = 0
        prefix = bytearray()
        try:
            for offset in range(0, len(value), BASE64_DECODE_CHARS):
                piece = value[offset:offset + BASE64_DECODE_CHARS]
                try:
                    compact = carry + "".join(character for character in piece if not character.isspace())
                except (TypeError, UnicodeError) as exc:
                    raise ResponseError(stage="decode") from exc
                decode_length = len(compact) - (len(compact) % 4)
                if decode_length == 0:
                    carry = compact
                    continue
                encoded = compact[:decode_length]
                carry = compact[decode_length:]
                if saw_padding and encoded:
                    raise ResponseError(stage="decode")
                if "=" in encoded:
                    padding_at = encoded.find("=")
                    padding = encoded[padding_at:]
                    if len(padding) > 2 or padding.strip("=") or carry:
                        raise ResponseError(stage="decode")
                    saw_padding = True
                try:
                    decoded = base64.b64decode(encoded, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise ResponseError(stage="decode") from exc
                self._ensure_disk_space(len(decoded))
                _write_all(descriptor, decoded)
                if len(prefix) < 16:
                    prefix.extend(decoded[:16 - len(prefix)])
                size += len(decoded)
            if carry:
                if saw_padding:
                    raise ResponseError(stage="decode")
                try:
                    decoded = base64.b64decode(carry, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise ResponseError(stage="decode") from exc
                self._ensure_disk_space(len(decoded))
                _write_all(descriptor, decoded)
                if len(prefix) < 16:
                    prefix.extend(decoded[:16 - len(prefix)])
                size += len(decoded)
            if size == 0:
                raise ResponseError(stage="decode")
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            self._commit_temporary(temporary, identity, size, bytes(prefix), metadata)
            committed = True
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if not committed:
                try:
                    _unlink_owned(temporary, identity)
                except (FileNotFoundError, OSError):
                    pass

    def publish(self) -> list[Path]:
        if self.expected_count is not None and self.count != self.expected_count:
            raise ValueError("image batch is incomplete")
        if not self.count:
            raise ValueError("image batch is empty")
        return self._publish_staged()

    def publish_partial(self) -> list[Path]:
        if not self.count or (self.expected_count is not None and self.count >= self.expected_count):
            raise ValueError("image batch is not partial")
        return self._publish_staged()

    def _publish_staged(self) -> list[Path]:
        assert self.staging is not None
        assert self.staging_identity is not None
        directory_info = self.directory.lstat()
        stage_info = self.staging.lstat()
        if (
            _is_link_like(directory_info)
            or (directory_info.st_dev, directory_info.st_ino) != self.directory_identity
            or _is_link_like(stage_info)
            or not stat.S_ISDIR(stage_info.st_mode)
            or (stage_info.st_dev, stage_info.st_ino) != self.staging_identity
        ):
            raise OSError("publication paths changed unexpectedly")
        final_batch: Optional[Path] = None
        for _ in range(20):
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
            candidate = self.directory / (
                "portdan-image-{}-{}".format(stamp, secrets.token_hex(16))
            )
            if _path_exists(candidate):
                continue
            final_batch = candidate
            break
        if final_batch is None:
            self.preserve_recovery = True
            raise OutputRecoveryError(self._recovery_path())
        try:
            if _path_exists(final_batch):
                raise FileExistsError()
            os.rename(str(self.staging), str(final_batch))
        except (FileExistsError, OSError):
            self.preserve_recovery = True
            raise OutputRecoveryError(self._recovery_path()) from None
        published_info = final_batch.lstat()
        if (
            _is_link_like(published_info)
            or not stat.S_ISDIR(published_info.st_mode)
            or (published_info.st_dev, published_info.st_ino) != self.staging_identity
        ):
            self.preserve_recovery = True
            raise OutputRecoveryError(final_batch.absolute())
        self.staging = None
        self.published = True
        paths = [final_batch / entry[0].name for entry in self.entries]
        self._published_artifacts = [
            Artifact(path.absolute(), image_format, size)
            for path, (_temporary, _identity, size, image_format) in zip(paths, self.entries)
        ]
        return paths

    def _recovery_path(self) -> Path:
        assert self.staging is not None
        return self.staging.absolute()

    def _cleanup_staging(self) -> None:
        if self.staging is None:
            return
        for temporary, identity, _size, _format in self.entries:
            try:
                _unlink_owned(temporary, identity)
            except (FileNotFoundError, OSError):
                pass
        try:
            for candidate in self.staging.iterdir():
                try:
                    info = candidate.lstat()
                    if stat.S_ISREG(info.st_mode) and not _is_link_like(info):
                        candidate.unlink()
                except (FileNotFoundError, OSError):
                    pass
            self.staging.rmdir()
        except (FileNotFoundError, OSError):
            pass


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _save_png(data: bytes) -> Path:
    with _ImageBatch(1) as batch:
        batch.stage_png(data)
        return batch.publish()[0]


def _unlink_owned(path: Path, identity: tuple[int, int]) -> None:
    current = path.lstat()
    if stat.S_ISREG(current.st_mode) and identity == (current.st_dev, current.st_ino):
        path.unlink()


def _stdin_source() -> Any:
    return getattr(sys.stdin, "buffer", sys.stdin)


def _prompt_from_source(source: Any) -> str:
    raw = source.readline(MAX_REQUEST_BODY_BYTES + 1)
    if isinstance(raw, bytes):
        try:
            prompt = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError() from exc
    else:
        prompt = str(raw)
    if len(prompt.encode("utf-8")) > MAX_REQUEST_BODY_BYTES:
        raise InputError("request_body_too_large", "input")
    prompt = prompt.rstrip("\r\n")
    if not prompt:
        raise InputError("invalid_prompt", "input")
    return prompt


def _prompt_from_stdin() -> str:
    return _prompt_from_source(_stdin_source())


def _prompt_and_key_from_stdin() -> tuple[str, str]:
    try:
        is_tty = bool(sys.stdin.isatty())
    except (AttributeError, OSError, ValueError):
        raise ProvidedKeyError() from None
    if is_tty:
        raise ProvidedKeyError()
    source = _stdin_source()
    prompt = _prompt_from_source(source)
    raw = source.readline(MAX_KEY_BYTES + 1)
    if isinstance(raw, bytes):
        if len(raw) > MAX_KEY_BYTES:
            raise ProvidedKeyError()
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProvidedKeyError() from exc
    else:
        value = str(raw)
        try:
            encoded_length = len(value.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ProvidedKeyError() from exc
        if encoded_length > MAX_KEY_BYTES:
            raise ProvidedKeyError()
    try:
        api_key = _key(value)
    except ConfigError as exc:
        raise ProvidedKeyError() from exc
    return prompt, api_key


def _read_limited_source(source: Any, limit: int) -> bytes:
    raw = source.read(limit + 1)
    if isinstance(raw, bytes):
        data = raw
    else:
        try:
            data = str(raw).encode("utf-8")
        except UnicodeEncodeError as exc:
            raise InputError("invalid_utf8", "input") from exc
    if len(data) > limit:
        raise LocalResourceError("request_body_too_large", "input")
    return data


def _parse_request_json(raw: bytes) -> Dict[str, Any]:
    def object_from_pairs(pairs: list[tuple[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise InputError("duplicate_request_field", "input")
            result[key] = value
        return result

    def reject_constant(_value: str) -> Any:
        raise InputError("invalid_request_json", "input")

    try:
        value = json.loads(
            raw.decode("utf-8-sig"),
            object_pairs_hook=object_from_pairs,
            parse_constant=reject_constant,
        )
    except InputError:
        raise
    except (UnicodeDecodeError, ValueError) as exc:
        raise InputError("invalid_request_json", "input") from exc
    if not isinstance(value, dict):
        raise InputError("request_json_not_object", "input")
    return value


def _request_json_from_stdin() -> Dict[str, Any]:
    return _parse_request_json(_read_limited_source(_stdin_source(), MAX_REQUEST_BODY_BYTES))


def _request_json_and_key_from_stdin() -> tuple[Dict[str, Any], str]:
    try:
        is_tty = bool(sys.stdin.isatty())
    except (AttributeError, OSError, ValueError):
        raise ProvidedKeyError() from None
    if is_tty:
        raise ProvidedKeyError()
    source = _stdin_source()
    raw_request = source.readline(MAX_REQUEST_BODY_BYTES + 1)
    if isinstance(raw_request, str):
        try:
            request_bytes = raw_request.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise InputError("invalid_utf8", "input") from exc
    else:
        request_bytes = raw_request
    if len(request_bytes) > MAX_REQUEST_BODY_BYTES:
        raise LocalResourceError("request_body_too_large", "input")
    request_value = _parse_request_json(request_bytes)
    raw_key = source.readline(MAX_KEY_BYTES + 1)
    if isinstance(raw_key, bytes):
        if len(raw_key) > MAX_KEY_BYTES:
            raise ProvidedKeyError()
        try:
            key_value = raw_key.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProvidedKeyError() from exc
    else:
        key_value = str(raw_key)
        if len(key_value.encode("utf-8")) > MAX_KEY_BYTES:
            raise ProvidedKeyError()
    trailing = source.read(1)
    if trailing not in (b"", ""):
        raise InputError("trailing_stdin_data", "input")
    try:
        api_key = _key(key_value)
    except ConfigError as exc:
        raise ProvidedKeyError() from exc
    return request_value, api_key


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise InputError("invalid_arguments", "arguments")


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(description="Call Portdan's GPT Images generations endpoint")
    parser.add_argument("--prompt-stdin", action="store_true", help="read the visual prompt from standard input")
    parser.add_argument(
        "--request-json-stdin",
        action="store_true",
        help="read one complete Images API JSON object from standard input",
    )
    parser.add_argument(
        "--api-key-stdin",
        action="store_true",
        help="read a one-time API Key using the documented non-TTY stdin framing",
    )
    parser.add_argument("--model")
    parser.add_argument("--size")
    parser.add_argument("--quality")
    parser.add_argument(
        "--count",
        type=int,
        help="Images API integer n value; forwarded without a client-side allowlist",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        help="optional legacy-compatible overall deadline in seconds; disabled by default",
    )
    parser.add_argument(
        "--json-memory-limit-mib",
        type=float,
        default=DEFAULT_JSON_MEMORY_LIMIT_BYTES / (1024 * 1024),
        help="local memory guard for one upstream JSON object/event",
    )
    parser.add_argument(
        "--proxy-mode",
        choices=PROXY_MODES,
        default="direct",
        help="direct ignores system proxies; system uses the host proxy configuration",
    )
    parser.add_argument("--json", action="store_true", help="emit one stable JSON result object")
    parser.add_argument("--diagnose", action="store_true", help="print safe local runtime diagnostics only")
    return parser


def _error_message(error: RequestError) -> str:
    if error.kind == "dns":
        message = "Portdan 域名解析失败；未收到 HTTP 响应，本次未自动重试"
    elif error.kind == "tls":
        message = "与 Portdan 建立 TLS 连接失败；未收到 HTTP 响应，本次未自动重试"
    elif error.kind == "timeout":
        message = "等待 Portdan 响应超时；未收到 HTTP 响应，请求是否已受理未知，本次未自动重试"
    elif error.kind in ("response_timeout", "idle_timeout"):
        message = "Portdan 图片流长时间没有数据，触发空闲超时；图片结果未知，本次未自动重试"
    elif error.kind == "overall_timeout":
        message = "等待 Portdan 图片流达到总时限；图片结果未知，本次未自动重试"
    elif error.kind == "truncated_stream":
        message = "Portdan 图片流在完成事件前结束；图片结果未知，本次未自动重试"
    elif error.kind == "stream_error":
        message = "Portdan 图片流返回失败事件；图片未保存，本次未自动重试"
    elif error.kind in ("connect", "transport"):
        if error.response_started:
            message = "已收到 Portdan HTTP 响应头，但连接在响应完成前中断；图片结果未知，本次未自动重试"
        else:
            message = "连接 Portdan 失败；未收到 HTTP 响应，请求是否已送达未知，本次未自动重试"
    elif error.status in (401, 403):
        message = "Portdan 拒绝了认证或当前分组未授权图片请求；本次未自动重试"
    elif error.status == 429:
        message = "Portdan 当前限流；本次未自动重试"
    elif error.status == 404:
        message = "Portdan 返回 404，图片请求未完成；本次未自动重试"
    elif error.status >= 500:
        message = "Portdan 返回 HTTP {}；图片结果未知，本次未自动重试".format(
            error.status
        )
    else:
        message = "Portdan 返回 HTTP {} 并拒绝了图片请求；本次未自动重试".format(
            error.status
        )
    if error.request_id:
        message += "；请求 ID：{}".format(error.request_id)
    if error.stage:
        message += "；阶段：{}".format(error.stage)
    if error.elapsed is not None:
        message += "；耗时：{:.1f} 秒".format(error.elapsed)
    return message


def _source_label(request_config: RequestConfig) -> str:
    return KEY_SOURCE_LABELS.get(request_config.source, KEY_SOURCE_LABELS["unknown"])


RESULT_SCHEMA = "portdan-image2.result.v1"


def _result_payload(
    *,
    status: str,
    error_code: Optional[str],
    error_stage: Optional[str],
    request_id: Optional[str],
    requested: Optional[int],
    artifacts: list[Artifact],
    elapsed: float,
    diagnostics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    error = None
    if error_code is not None:
        error = {"code": error_code, "stage": error_stage or "unknown"}
    return {
        "schema": RESULT_SCHEMA,
        "status": status,
        "error": error,
        "request_id": request_id,
        "requested": requested,
        "completed": len(artifacts),
        "artifacts": [
            {"path": str(item.path), "format": item.format, "bytes": item.bytes}
            for item in artifacts
        ],
        "diagnostics": diagnostics,
        "elapsed_seconds": round(max(0.0, elapsed), 3),
    }


def _emit_json_result(**kwargs: Any) -> None:
    print(json.dumps(_result_payload(**kwargs), ensure_ascii=False, separators=(",", ":")))


def main(argv: Optional[list[str]] = None) -> int:
    argv_list = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in argv_list
    started = time.monotonic()
    request_config: Optional[RequestConfig] = None
    client_request_id: Optional[str] = None
    requested: Optional[int] = None
    artifacts: list[Artifact] = []

    def emit(status: str, code: Optional[str] = None, stage: Optional[str] = None) -> None:
        if json_mode:
            _emit_json_result(
                status=status,
                error_code=code,
                error_stage=stage,
                request_id=client_request_id,
                requested=requested,
                artifacts=artifacts,
                elapsed=time.monotonic() - started,
                diagnostics=None,
            )

    try:
        args = _parser().parse_args(argv_list)
        json_mode = args.json
        if args.timeout is not None and (not math.isfinite(args.timeout) or args.timeout <= 0):
            raise InputError("invalid_timeout", "arguments")
        if (
            not math.isfinite(args.json_memory_limit_mib)
            or args.json_memory_limit_mib <= 0
        ):
            raise InputError("invalid_memory_limit", "arguments")
        json_memory_limit = int(args.json_memory_limit_mib * 1024 * 1024)
        if json_memory_limit <= 0:
            raise InputError("invalid_memory_limit", "arguments")
        if args.diagnose:
            try:
                diagnostic_config = resolve_request_config()
                diagnostic_key_source = diagnostic_config.source
            except ConfigError:
                diagnostic_key_source = "missing"
            diagnostics = {
                "endpoint": ENDPOINT,
                "key_source": diagnostic_key_source,
                "output_directory": str((Path.cwd() / "portdan-images").absolute()),
            }
            if args.json:
                _emit_json_result(
                    status="diagnose",
                    error_code=None,
                    error_stage=None,
                    request_id=None,
                    requested=None,
                    artifacts=[],
                    elapsed=time.monotonic() - started,
                    diagnostics=diagnostics,
                )
            else:
                print(
                    "endpoint={}；Key 来源={}；输出目录={}".format(
                        diagnostics["endpoint"],
                        diagnostics["key_source"],
                        diagnostics["output_directory"],
                    )
                )
            return 0

        legacy_prompt_mode = args.prompt_stdin or (
            args.api_key_stdin and not args.request_json_stdin
        )
        input_modes = int(legacy_prompt_mode) + int(args.request_json_stdin)
        if input_modes != 1:
            raise InputError("invalid_input_mode", "arguments")
        if args.request_json_stdin and any(
            value is not None
            for value in (args.model, args.size, args.quality, args.count)
        ):
            raise InputError("conflicting_request_options", "arguments")
        if legacy_prompt_mode and args.count is not None:
            requested = _usable_positive_integer(args.count)
        if legacy_prompt_mode:
            if args.model is not None:
                _validate_image_model(args.model)
            if args.api_key_stdin:
                prompt, provided_api_key = _prompt_and_key_from_stdin()
                request_config = RequestConfig(
                    api_key=provided_api_key,
                    source=KEY_SOURCE_STDIN,
                )
            else:
                prompt = _prompt_from_stdin()
            payload_args = (
                request_config or RequestConfig(api_key=""),
                prompt,
                args.size,
                args.quality,
                args.count,
            )
            body = (
                _payload(*payload_args)
                if args.model is None
                else _payload(*payload_args, model=args.model)
            )
        else:
            if args.api_key_stdin:
                request_value, provided_api_key = _request_json_and_key_from_stdin()
                request_config = RequestConfig(
                    api_key=provided_api_key,
                    source=KEY_SOURCE_STDIN,
                )
            else:
                request_value = _request_json_from_stdin()
            body, requested = _raw_payload(request_value)

        if request_config is None:
            request_config = resolve_request_config()
        client_request_id = _new_request_id()
        print(
            "正在通过 Portdan 调用 GPT Images；Key 来源：{}；请求 ID：{}".format(
                _source_label(request_config),
                client_request_id,
            ),
            file=sys.stderr,
            flush=True,
        )
        with _ImageBatch(requested) as batch:
            result = _post(
                request_config.api_key,
                body,
                args.timeout,
                proxy_mode=args.proxy_mode,
                client_request_id=client_request_id,
                expected_count=requested,
                on_image=batch.stage_payload,
                json_memory_limit=json_memory_limit,
            )
            if isinstance(result, bytes):
                result = PostResult(body=result, request_id=client_request_id)
            if result.body and batch.count == 0:
                _consume_image_response(
                    result.body,
                    expected_count=requested,
                    on_image=batch.stage_payload,
                    request_id=result.request_id,
                    elapsed=result.first_event_seconds,
                )
            outputs = [path.absolute() for path in batch.publish()]
            artifacts = batch.artifacts
        if not args.json:
            for output in outputs:
                print(output)
        print(
            "已生成 {} 个 artifact，耗时 {:.1f} 秒；请求 ID：{}".format(
                len(artifacts),
                time.monotonic() - started,
                result.request_id,
            ),
            file=sys.stderr,
        )
        client_request_id = result.request_id
        emit("completed")
        return 0
    except ConfigError:
        print(MISSING_KEY_MESSAGE, file=sys.stderr)
        emit("error", "missing_api_key", "auth")
        return 2
    except ProvidedKeyError:
        print(PROVIDED_KEY_MESSAGE, file=sys.stderr)
        emit("error", "invalid_api_key_input", "input")
        return 2
    except InputError as exc:
        print("图片请求参数或输入格式无效；阶段：{}".format(exc.stage), file=sys.stderr)
        emit("error", exc.code, exc.stage)
        return 2
    except LocalResourceError as exc:
        print("本机资源保护阻止了图片请求或保存；阶段：{}".format(exc.stage), file=sys.stderr)
        emit("error", exc.code, exc.stage)
        return 6
    except ValueError:
        print("图片请求参数或输入格式无效", file=sys.stderr)
        emit("error", "invalid_input", "input")
        return 2
    except RequestError as exc:
        client_request_id = exc.request_id or client_request_id
        message = _error_message(exc)
        if request_config is not None:
            message += "；Key 来源：{}".format(_source_label(request_config))
        print(message, file=sys.stderr)
        emit("error", exc.kind, exc.stage)
        return 3 if exc.status in (401, 403) else 4
    except PartialImageError as exc:
        client_request_id = exc.request_id or client_request_id
        batch = exc.staged_batch
        if not isinstance(batch, _ImageBatch):
            print("Portdan 返回了部分图片，但无法安全恢复已验证批次", file=sys.stderr)
            emit("error", "partial_recovery_failed", "publish")
            return 6
        try:
            partial_outputs = [path.absolute() for path in batch.publish_partial()]
            artifacts = batch.artifacts
        except OutputRecoveryError:
            print("无法安全发布已完成的部分图片", file=sys.stderr)
            emit("error", "publish_failed", "publish")
            return 6
        except (OSError, OutputWriteError):
            print("无法安全保存已完成的部分图片", file=sys.stderr)
            emit("error", "output_error", "save")
            return 6
        if not json_mode:
            for output in partial_outputs:
                print(output)
        expected_label = "?" if exc.expected is None else str(exc.expected)
        print(
            "Portdan 流中断：已安全发布 {}/{} 个完整 artifact，本次未自动补发；请求 ID：{}".format(
                exc.completed,
                expected_label,
                client_request_id or "unknown",
            ),
            file=sys.stderr,
        )
        emit("partial")
        return 7
    except ResponseError as exc:
        client_request_id = exc.request_id or client_request_id
        print("Portdan 返回的图片数据无效；阶段：{}".format(exc.stage), file=sys.stderr)
        emit("error", "invalid_response", exc.stage)
        return 5
    except OutputRecoveryError:
        print("无法安全发布图片；完整恢复批次已保留在本机", file=sys.stderr)
        emit("error", "publish_failed", "publish")
        return 6
    except (OSError, OutputWriteError):
        print("无法安全保存生成的图片", file=sys.stderr)
        emit("error", "output_error", "save")
        return 6
    except Exception:
        print("图片运行时发生未分类错误；未输出原始异常内容", file=sys.stderr)
        emit("error", "runtime_error", "runtime")
        return 6
if __name__ == "__main__":
    raise SystemExit(main())
