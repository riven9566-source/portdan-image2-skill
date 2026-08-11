from __future__ import annotations

import base64
import contextlib
import errno
import hashlib
import http.client
import io
import json
import os
import socket
import sqlite3
import ssl
import tempfile
import threading
import time
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "skill" / "portdan-image2" / "scripts"
import sys

sys.path.insert(0, str(SCRIPT_DIR))
import generate_image  # noqa: E402


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
KEY = "portdan-test-key-123456"
OTHER_KEY = "portdan-other-key-654321"
MODEL = "gpt-5.6-sol"
MISSING = object()


class TtyInput(io.StringIO):
    def isatty(self) -> bool:
        return True


class UnverifiableTtyInput(io.StringIO):
    def isatty(self) -> bool:
        raise OSError("unable to determine terminal state")


class HTTPBodyResponse(io.BytesIO):
    status = 200

    def __init__(
        self,
        body: bytes,
        *,
        content_type: str = "application/json",
        request_id: str = "pdi-server-echo",
    ) -> None:
        super().__init__(body)
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.headers["X-Request-ID"] = request_id

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self):
        return self.status


def response_body() -> bytes:
    return json.dumps({
        "status": "completed",
        "output": [{
            "type": "image_generation_call",
            "status": "completed",
            "result": base64.b64encode(PNG).decode("ascii"),
        }],
    }).encode()


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        len(payload).to_bytes(4, "big") + kind + payload
        + (generate_image.zlib.crc32(kind + payload) & 0xFFFFFFFF).to_bytes(4, "big")
    )


def colored_png(red: int, green: int, blue: int) -> bytes:
    ihdr = (
        (1).to_bytes(4, "big")
        + (1).to_bytes(4, "big")
        + bytes((8, 6, 0, 0, 0))
    )
    pixels = bytes((0, red, green, blue, 255))
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", ihdr)
        + png_chunk(b"IDAT", generate_image.zlib.compress(pixels))
        + png_chunk(b"IEND", b"")
    )


def large_pixel_png() -> bytes:
    width = 2049
    height = 2049
    ihdr = (
        width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + bytes((1, 0, 0, 0, 0))
    )
    row = b"\x00" + bytes((width + 7) // 8)
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", ihdr)
        + png_chunk(b"IDAT", generate_image.zlib.compress(row * height))
        + png_chunk(b"IEND", b"")
    )


class GenerateImageTests(unittest.TestCase):
    def write_config(self, root: Path, content: str, auth: dict | None = None) -> None:
        root.mkdir(parents=True, exist_ok=True)
        (root / "config.toml").write_text(content, encoding="utf-8")
        if auth is not None:
            (root / "auth.json").write_text(json.dumps(auth), encoding="utf-8")

    def write_cc_database(
        self,
        home: Path,
        config: str,
        auth: dict,
        current: int = 1,
        name: str = "Portdan",
        provider_id: str = "provider-1",
        website_url: str | None = None,
        current_provider_id: object = MISSING,
    ) -> None:
        self.write_cc_database_rows(
            home,
            [{
                "id": provider_id,
                "current": current,
                "name": name,
                "website_url": website_url,
                "config": config,
                "auth": auth,
            }],
            current_provider_id=current_provider_id,
        )

    def write_cc_database_rows(
        self,
        home: Path,
        rows: list[dict],
        *,
        current_provider_id: object = MISSING,
        include_id: bool = True,
    ) -> None:
        directory = home / ".cc-switch"
        directory.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(directory / "cc-switch.db")
        try:
            columns = [
                "app_type TEXT",
                "is_current INTEGER",
                "name TEXT",
                "website_url TEXT",
                "settings_config TEXT",
            ]
            if include_id:
                columns.insert(0, "id TEXT")
            connection.execute("CREATE TABLE providers ({})".format(", ".join(columns)))
            for row in rows:
                values = [
                    "codex",
                    row.get("current", 0),
                    row.get("name", "Custom provider"),
                    row.get("website_url"),
                    json.dumps({
                        "config": row.get("config", ""),
                        "auth": row.get("auth", {}),
                    }),
                ]
                if include_id:
                    values.insert(0, row.get("id"))
                placeholders = ", ".join("?" for _ in values)
                connection.execute(
                    "INSERT INTO providers VALUES ({})".format(placeholders), values
                )
            connection.commit()
        finally:
            connection.close()
        if current_provider_id is not MISSING:
            (directory / "settings.json").write_text(
                json.dumps({"currentProviderCodex": current_provider_id}),
                encoding="utf-8",
            )

    def resolve(self, home: Path, env: dict[str, str] | None = None) -> str:
        with patch.object(Path, "home", return_value=home), patch.dict(
            os.environ, env or {}, clear=True
        ):
            return generate_image.resolve_api_key()

    def resolve_request(
        self, home: Path, env: dict[str, str] | None = None
    ) -> generate_image.RequestConfig:
        with patch.object(Path, "home", return_value=home), patch.dict(
            os.environ, env or {}, clear=True
        ):
            return generate_image.resolve_request_config()

    def test_cc_switch_current_codex_provider_is_first_key_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_cc_database(
                home,
                'model = "{}"\nopenai_base_url = "https://portdan.com"\n'.format(MODEL),
                {"auth_mode": "apikey", "OPENAI_API_KEY": KEY},
            )
            self.write_config(
                home / ".codex",
                '[model_providers.portdan]\nbase_url = "https://portdan.com"\n'
                'experimental_bearer_token = "{}"\n'.format(OTHER_KEY),
            )
            self.assertEqual(self.resolve(home, {"PORTDAN_API_KEY": OTHER_KEY}), KEY)
            self.assertEqual(
                self.resolve_request(home, {"PORTDAN_API_KEY": OTHER_KEY}).model,
                MODEL,
            )

    def test_cc_switch_current_provider_website_url_can_prove_portdan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_cc_database(
                home,
                "this config is intentionally not parseable",
                {"OPENAI_API_KEY": KEY},
                name="Customer-defined name",
                website_url="https://portdan.com/v1/responses",
            )
            request_config = self.resolve_request(home)
            self.assertEqual(request_config.api_key, KEY)
            self.assertEqual(request_config.source, generate_image.KEY_SOURCE_CC_SWITCH)

    def test_provider_name_alone_does_not_authorize_its_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_cc_database(
                home,
                "this provider config is not parseable",
                {"OPENAI_API_KEY": OTHER_KEY},
                name="Portdan",
            )
            request_config = self.resolve_request(home, {"PORTDAN_API_KEY": KEY})
            self.assertEqual(request_config.api_key, KEY)
            self.assertEqual(request_config.source, generate_image.KEY_SOURCE_ENV)

    def test_non_portdan_current_cc_switch_key_is_not_sent_to_portdan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_cc_database(
                home,
                'openai_base_url = "https://api.openai.com/v1"\n',
                {"OPENAI_API_KEY": OTHER_KEY},
                name="OpenAI",
            )
            self.assertEqual(self.resolve(home, {"PORTDAN_API_KEY": KEY}), KEY)

    def test_non_current_cc_switch_provider_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_cc_database(
                home,
                'openai_base_url = "https://portdan.com"\n',
                {"OPENAI_API_KEY": OTHER_KEY},
                current=0,
            )
            self.assertEqual(self.resolve(home, {"PORTDAN_API_KEY": KEY}), KEY)

    def test_current_provider_codex_overrides_stale_is_current_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_cc_database_rows(
                home,
                [{
                    "id": "chosen-customer-provider",
                    "current": 0,
                    "name": "Customer billing route",
                    "config": 'openai_base_url = "https://portdan.com/v1/responses"\n',
                    "auth": {"CODEX_API_KEY": KEY},
                }, {
                    "id": "stale-row",
                    "current": 1,
                    "name": "Old OpenAI",
                    "config": 'openai_base_url = "https://api.openai.com/v1"\n',
                    "auth": {"OPENAI_API_KEY": OTHER_KEY},
                }],
                current_provider_id="chosen-customer-provider",
            )
            request_config = self.resolve_request(home)
            self.assertEqual(request_config.api_key, KEY)
            self.assertEqual(request_config.source, generate_image.KEY_SOURCE_CC_SWITCH)

    def test_selected_foreign_provider_does_not_fall_back_to_stale_portdan_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_cc_database_rows(
                home,
                [{
                    "id": "selected-foreign",
                    "current": 0,
                    "config": 'openai_base_url = "https://api.openai.com/v1"\n',
                    "auth": {"OPENAI_API_KEY": OTHER_KEY},
                }, {
                    "id": "stale-portdan",
                    "current": 1,
                    "config": 'openai_base_url = "https://portdan.com/v1"\n',
                    "auth": {"OPENAI_API_KEY": OTHER_KEY},
                }],
                current_provider_id="selected-foreign",
            )
            request_config = self.resolve_request(home, {"PORTDAN_API_KEY": KEY})
            self.assertEqual(request_config.api_key, KEY)
            self.assertEqual(request_config.source, generate_image.KEY_SOURCE_ENV)

    def test_current_provider_codex_compatibility_fallbacks_use_is_current(self) -> None:
        scenarios = (
            (MISSING, True),
            ("missing-provider-id", True),
            (["not", "a", "string"], True),
            ("provider-1", False),
        )
        for current_provider_id, include_id in scenarios:
            with self.subTest(
                current_provider_id=current_provider_id, include_id=include_id
            ), tempfile.TemporaryDirectory() as temp:
                home = Path(temp)
                self.write_cc_database_rows(
                    home,
                    [{
                        "id": "provider-1",
                        "current": 1,
                        "name": "Arbitrary name",
                        "config": 'openai_base_url = "https://portdan.com/v1"\n',
                        "auth": {"API_KEY": KEY},
                    }],
                    current_provider_id=current_provider_id,
                    include_id=include_id,
                )
                self.assertEqual(self.resolve(home), KEY)

    def test_current_provider_codex_requires_an_exact_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_cc_database_rows(
                home,
                [{
                    "id": "chosen-provider",
                    "current": 0,
                    "config": 'openai_base_url = "https://portdan.com/v1"\n',
                    "auth": {"OPENAI_API_KEY": KEY},
                }],
                current_provider_id=" chosen-provider ",
            )
            request_config = self.resolve_request(home, {"PORTDAN_API_KEY": OTHER_KEY})
            self.assertEqual(request_config.api_key, OTHER_KEY)
            self.assertEqual(request_config.source, generate_image.KEY_SOURCE_ENV)

    def test_current_provider_id_lookup_does_not_require_is_current_column(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            directory = home / ".cc-switch"
            directory.mkdir()
            connection = sqlite3.connect(directory / "cc-switch.db")
            try:
                connection.execute(
                    "CREATE TABLE providers "
                    "(id TEXT, app_type TEXT, website_url TEXT, settings_config TEXT)"
                )
                connection.execute(
                    "INSERT INTO providers VALUES (?, ?, ?, ?)",
                    (
                        "selected-provider",
                        "codex",
                        "https://portdan.com/v1/responses",
                        json.dumps({
                            "config": "invalid TOML",
                            "auth": {"API_KEY": KEY},
                        }),
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            (directory / "settings.json").write_text(
                json.dumps({"currentProviderCodex": "selected-provider"}),
                encoding="utf-8",
            )
            self.assertEqual(self.resolve(home), KEY)

    def test_is_current_fallback_uses_only_the_latest_matching_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_cc_database_rows(
                home,
                [{
                    "id": "stale-portdan",
                    "current": 1,
                    "config": 'openai_base_url = "https://portdan.com/v1"\n',
                    "auth": {"OPENAI_API_KEY": OTHER_KEY},
                }, {
                    "id": "latest-foreign",
                    "current": 1,
                    "config": 'openai_base_url = "https://api.openai.com/v1"\n',
                    "auth": {"OPENAI_API_KEY": OTHER_KEY},
                }],
            )
            request_config = self.resolve_request(home, {"PORTDAN_API_KEY": KEY})
            self.assertEqual(request_config.api_key, KEY)
            self.assertEqual(request_config.source, generate_image.KEY_SOURCE_ENV)

    def test_portdan_url_allowlist_accepts_supported_paths_only(self) -> None:
        accepted = (
            "https://portdan.com",
            "https://portdan.com/",
            "https://portdan.com:443/v1",
            "https://portdan.com/v1/",
            "https://portdan.com/v1/responses",
            "https://portdan.com/v1/responses/",
            "https://portdan.com/v1/images/generations",
            "https://portdan.com/v1/images/generations/",
            "https://portdan.com/backend-api/codex",
            "https://portdan.com/backend-api/codex/",
        )
        rejected = (
            "http://portdan.com/v1",
            "https://portdan.com:444/v1",
            "https://portdan.com.evil/v1",
            "https://example.com/https://portdan.com/v1",
            "https://user@portdan.com/v1",
            "https://portdan.com/v1/other",
            "https://portdan.com/v1////",
            "https://portdan.com/v1?key=value",
            "https://portdan.com/v1#fragment",
            " https://portdan.com/v1",
            "https://portdan.com/v1 ",
        )
        for value in accepted:
            with self.subTest(value=value):
                self.assertTrue(generate_image._is_portdan_url(value))
        for value in rejected:
            with self.subTest(value=value):
                self.assertFalse(generate_image._is_portdan_url(value))

    def test_cc_switch_explicit_auth_key_aliases_are_supported(self) -> None:
        for field_name in generate_image.PROVIDER_KEY_FIELDS:
            with self.subTest(field_name=field_name), tempfile.TemporaryDirectory() as temp:
                home = Path(temp)
                self.write_cc_database(
                    home,
                    "invalid toml; website URL supplies provider identity",
                    {field_name: KEY},
                    name="Customer alias",
                    website_url="https://portdan.com/v1",
                )
                request_config = self.resolve_request(home)
                self.assertEqual(request_config.api_key, KEY)
                self.assertEqual(
                    request_config.source, generate_image.KEY_SOURCE_CC_SWITCH
                )

    def test_unknown_auth_and_provider_token_fields_are_not_guessed(self) -> None:
        unknown_auth_fields = (
            "access_token",
            "refresh_token",
            "token",
            "bearer_token",
            "authorization",
            "secret",
        )
        for field_name in unknown_auth_fields:
            with self.subTest(field_name=field_name), tempfile.TemporaryDirectory() as temp:
                home = Path(temp)
                self.write_cc_database(
                    home,
                    'openai_base_url = "https://portdan.com/v1"\n',
                    {field_name: OTHER_KEY},
                    name="Customer alias",
                )
                with self.assertRaises(generate_image.ConfigError):
                    self.resolve(home)

        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_config(
                home / ".codex",
                '[model_providers.customer_alias]\n'
                'base_url = "https://portdan.com/v1"\n'
                'access_token = "{}"\n'
                'refresh_token = "{}"\n'
                'token = "{}"\n'.format(KEY, KEY, KEY),
            )
            with self.assertRaises(generate_image.ConfigError):
                self.resolve(home)

    def test_cc_switch_database_config_inline_token_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_cc_database(
                home,
                '[model_providers.any_name]\nbase_url = "https://portdan.com/v1"\n'
                'experimental_bearer_token = "{}"\n'.format(KEY),
                {},
                name="Not Portdan",
            )
            self.assertEqual(self.resolve(home), KEY)
            self.assertEqual(
                self.resolve_request(home).model, generate_image.DEFAULT_RESPONSE_MODEL
            )

    def test_known_incompatible_outer_models_use_safe_fallback(self) -> None:
        for model in (
            "gpt-image-2",
            "openai/gpt-image-2",
            "gpt-5.3-codex-spark",
            "gpt-5.3-codex-spark-high",
            "gpt5.3codexspark",
        ):
            with self.subTest(model=model):
                self.assertEqual(
                    generate_image._configured_model({"model": model}),
                    generate_image.DEFAULT_RESPONSE_MODEL,
                )
        self.assertEqual(generate_image._configured_model({"model": MODEL}), MODEL)

    def test_cc_switch_database_config_env_key_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_cc_database(
                home,
                '[model_providers.any_name]\nbase_url = "https://portdan.com/v1"\n'
                'env_key = "PORTDAN_TEST_KEY"\n',
                {},
            )
            self.assertEqual(self.resolve(home, {"PORTDAN_TEST_KEY": KEY}), KEY)

    def test_broken_cc_switch_database_falls_back_to_codex_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            database = home / ".cc-switch" / "cc-switch.db"
            database.parent.mkdir(parents=True)
            database.write_bytes(b"not sqlite")
            self.write_config(
                home / ".codex",
                '[model_providers.arbitrary]\nbase_url = "https://portdan.com/v1"\n'
                'experimental_bearer_token = "{}"\n'.format(KEY),
            )
            self.assertEqual(self.resolve(home), KEY)

    def test_inline_key_needs_no_portdan_provider_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_config(
                home / ".codex",
                '[model_providers.customer_alias]\n'
                'base_url = "https://portdan.com/v1/responses"\n'
                'experimental_bearer_token = "{}"\n'.format(KEY),
            )
            self.assertEqual(self.resolve(home), KEY)

    def test_codex_provider_explicit_inline_key_aliases_are_supported(self) -> None:
        for field_name in generate_image.PROVIDER_KEY_FIELDS:
            with self.subTest(field_name=field_name), tempfile.TemporaryDirectory() as temp:
                home = Path(temp)
                self.write_config(
                    home / ".codex",
                    '[model_providers.customer_alias]\n'
                    'base_url = "https://portdan.com/v1/responses"\n'
                    '{} = "{}"\n'.format(field_name, KEY),
                )
                request_config = self.resolve_request(home)
                self.assertEqual(request_config.api_key, KEY)
                self.assertEqual(request_config.source, generate_image.KEY_SOURCE_DEFAULT)

    def test_inline_portdan_token_does_not_read_auth_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_config(
                home / ".codex",
                '[model_providers.customer_alias]\n'
                'base_url = "https://portdan.com"\n'
                'experimental_bearer_token = "{}"\n'.format(KEY),
                {"OPENAI_API_KEY": OTHER_KEY},
            )
            with patch.object(generate_image, "_read_auth") as read_auth:
                self.assertEqual(self.resolve(home), KEY)
            read_auth.assert_not_called()

    def test_active_provider_auth_precedes_backup_inline_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_config(
                home / ".codex",
                'model_provider = "paid_group"\n'
                '[model_providers.paid_group]\nbase_url = "https://portdan.com/v1"\n'
                'requires_openai_auth = true\n'
                '[model_providers.portdan_backup]\n'
                'base_url = "https://portdan.com/v1"\n'
                'experimental_bearer_token = "{}"\n'.format(OTHER_KEY),
                {"OPENAI_API_KEY": KEY},
            )
            self.assertEqual(self.resolve(home), KEY)

    def test_top_level_portdan_auth_precedes_backup_inline_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_config(
                home / ".codex",
                'openai_base_url = "https://portdan.com"\n'
                '[model_providers.portdan_backup]\n'
                'experimental_bearer_token = "{}"\n'.format(OTHER_KEY),
                {"OPENAI_API_KEY": KEY},
            )
            self.assertEqual(self.resolve(home), KEY)

    def test_openai_base_url_uses_codex_auth_json_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_config(
                home / ".codex",
                'model = "{}"\nopenai_base_url = "https://portdan.com"\n'.format(MODEL),
                {"auth_mode": "apikey", "OPENAI_API_KEY": KEY, "tokens": {"access_token": "ignored"}},
            )
            self.assertEqual(self.resolve(home), KEY)
            self.assertEqual(self.resolve_request(home).model, MODEL)

    def test_codex_auth_json_explicit_key_aliases_are_supported(self) -> None:
        for field_name in generate_image.PROVIDER_KEY_FIELDS:
            with self.subTest(field_name=field_name), tempfile.TemporaryDirectory() as temp:
                home = Path(temp)
                self.write_config(
                    home / ".codex",
                    'openai_base_url = "https://portdan.com/v1/responses"\n',
                    {field_name: KEY},
                )
                request_config = self.resolve_request(home)
                self.assertEqual(request_config.api_key, KEY)
                self.assertEqual(request_config.source, generate_image.KEY_SOURCE_DEFAULT)

    def test_requires_openai_auth_provider_uses_auth_json_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_config(
                home / ".codex",
                'model_provider = "my_provider"\n[model_providers.my_provider]\n'
                'base_url = "https://portdan.com/v1/responses"\n'
                'requires_openai_auth = true\n',
                {"OPENAI_API_KEY": KEY},
            )
            self.assertEqual(self.resolve(home), KEY)

    def test_multiple_provider_keys_without_active_provider_are_not_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_config(
                home / ".codex",
                '[model_providers.one]\nexperimental_bearer_token = "{}"\n'
                '[model_providers.two]\nexperimental_bearer_token = "{}"\n'.format(
                    KEY, OTHER_KEY
                ),
            )
            fallback = "portdan-fallback-key-112233"
            self.assertEqual(self.resolve(home, {"PORTDAN_API_KEY": fallback}), fallback)

    def test_non_portdan_inline_key_is_not_sent_to_portdan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_config(
                home / ".codex",
                'model_provider = "openai"\n[model_providers.openai]\n'
                'base_url = "https://api.openai.com/v1"\n'
                'experimental_bearer_token = "{}"\n'.format(OTHER_KEY),
            )
            self.assertEqual(self.resolve(home, {"PORTDAN_API_KEY": KEY}), KEY)

    def test_active_foreign_provider_does_not_use_backup_portdan_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_config(
                home / ".codex",
                'model = "foreign-provider-only-model"\n'
                'model_provider = "foreign"\n'
                '[model_providers.foreign]\n'
                'base_url = "https://api.example.com/v1"\n'
                '[model_providers.portdan_backup]\n'
                'base_url = "https://portdan.com/v1"\n'
                'experimental_bearer_token = "{}"\n'.format(KEY),
            )
            with self.assertRaises(generate_image.ConfigError):
                self.resolve(home)

    def test_provider_env_key_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_config(
                home / ".codex",
                '[model_providers.portdan]\nbase_url = "https://portdan.com"\n'
                'env_key = "PORTDAN_TEST_KEY"\n',
            )
            self.assertEqual(self.resolve(home, {"PORTDAN_TEST_KEY": KEY}), KEY)

    def test_code_home_precedes_default_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            home.mkdir()
            custom = Path(temp) / "codex-home"
            self.write_config(
                custom,
                '[model_providers.portdan]\nbase_url = "https://portdan.com"\n'
                'experimental_bearer_token = "{}"\n'.format(KEY),
            )
            self.write_config(
                home / ".codex",
                '[model_providers.portdan]\nbase_url = "https://portdan.com"\n'
                'experimental_bearer_token = "{}"\n'.format(OTHER_KEY),
            )
            self.assertEqual(self.resolve(home, {"CODEX_HOME": str(custom)}), KEY)

    def test_installed_skill_codex_root_precedes_code_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            home.mkdir()
            installed = root / "installed codex"
            installed.mkdir()
            script = (
                installed
                / "skills"
                / "portdan-image2"
                / "scripts"
                / "generate_image.py"
            )
            self.write_config(
                installed,
                '[model_providers.local_alias]\nbase_url = "https://portdan.com/v1"\n'
                'experimental_bearer_token = "{}"\n'.format(KEY),
            )
            code_home = root / "code-home"
            self.write_config(
                code_home,
                '[model_providers.local_alias]\nbase_url = "https://portdan.com/v1"\n'
                'experimental_bearer_token = "{}"\n'.format(
                    OTHER_KEY
                ),
            )
            with patch.object(generate_image, "__file__", str(script)):
                self.assertEqual(
                    self.resolve(home, {"CODEX_HOME": str(code_home)}), KEY
                )

    def test_installed_skill_key_stops_before_custom_directory_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            home.mkdir()
            installed = root / "installed codex"
            installed.mkdir()
            script = installed / "skills" / "portdan-image2" / "scripts" / "generate_image.py"
            self.write_config(
                installed,
                '[model_providers.local_alias]\nbase_url = "https://portdan.com/v1"\n'
                'experimental_bearer_token = "{}"\n'.format(KEY),
            )
            with patch.object(generate_image, "__file__", str(script)), patch.object(
                generate_image, "_custom_codex_root"
            ) as custom:
                self.assertEqual(self.resolve(home), KEY)
            custom.assert_not_called()

    def test_short_cc_switch_write_lock_keeps_current_provider_priority(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_cc_database(
                home,
                'openai_base_url = "https://portdan.com"\n',
                {"OPENAI_API_KEY": KEY},
            )
            database = home / ".cc-switch" / "cc-switch.db"
            locker = sqlite3.connect(database, check_same_thread=False)
            locker.execute("PRAGMA journal_mode=DELETE")
            locker.execute("BEGIN EXCLUSIVE")
            release = threading.Timer(0.05, locker.rollback)
            release.start()
            try:
                self.assertEqual(
                    self.resolve(home, {"PORTDAN_API_KEY": OTHER_KEY}), KEY
                )
            finally:
                release.join()
                locker.close()

    def test_cc_switch_custom_directory_precedes_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            home.mkdir()
            custom = Path(temp) / "custom codex"
            self.write_config(
                custom,
                '[model_providers.portdan]\nbase_url = "https://portdan.com"\n'
                'experimental_bearer_token = "{}"\n'.format(KEY),
            )
            self.write_config(
                home / ".codex",
                '[model_providers.portdan]\nbase_url = "https://portdan.com"\n'
                'experimental_bearer_token = "{}"\n'.format(OTHER_KEY),
            )
            settings = home / ".cc-switch" / "settings.json"
            settings.parent.mkdir()
            settings.write_text(json.dumps({"codexConfigDir": str(custom)}), encoding="utf-8")
            self.assertEqual(self.resolve(home), KEY)

    def test_invalid_cc_switch_settings_do_not_block_default_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            settings = home / ".cc-switch" / "settings.json"
            settings.parent.mkdir()
            settings.write_text("not json", encoding="utf-8")
            self.write_config(
                home / ".codex",
                '[model_providers.portdan]\nbase_url = "https://portdan.com"\n'
                'experimental_bearer_token = "{}"\n'.format(KEY),
            )
            self.assertEqual(self.resolve(home), KEY)

    def test_portdan_api_key_environment_is_final_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(self.resolve(Path(temp), {"PORTDAN_API_KEY": KEY}), KEY)
            request_config = self.resolve_request(
                Path(temp), {"PORTDAN_API_KEY": KEY}
            )
            self.assertEqual(request_config.model, generate_image.DEFAULT_RESPONSE_MODEL)
            self.assertEqual(request_config.source, generate_image.KEY_SOURCE_ENV)

    def test_non_portdan_auth_json_is_not_used(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_config(
                home / ".codex",
                'openai_base_url = "https://api.openai.com/v1"\n',
                {"OPENAI_API_KEY": KEY},
            )
            with self.assertRaises(generate_image.ConfigError):
                self.resolve(home)

    def test_stale_top_level_portdan_url_does_not_override_active_foreign_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_config(
                home / ".codex",
                'openai_base_url = "https://portdan.com"\n'
                'model_provider = "other"\n'
                '[model_providers.other]\n'
                'base_url = "https://api.openai.com/v1"\n',
                {"OPENAI_API_KEY": OTHER_KEY},
            )
            self.assertEqual(self.resolve(home, {"PORTDAN_API_KEY": KEY}), KEY)

    def test_python39_fallback_supports_quoted_provider_and_auth_json(self) -> None:
        config = (
            'model_provider = "Portdan AI 聚合平台"\n'
            '[model_providers."Portdan AI 聚合平台"]\n'
            'base_url = "https://portdan.com"\nrequires_openai_auth = true\n'
        )
        with patch.object(generate_image, "tomllib", None):
            parsed = generate_image._parse_config(config.encode("utf-8"))
        self.assertEqual(
            generate_image._key_from_config(parsed, {"OPENAI_API_KEY": KEY}, {}), KEY
        )

    def test_python39_fallback_supports_dotted_provider_key_aliases(self) -> None:
        config = (
            'model_provider = "customer_alias"\n'
            'model_providers.customer_alias.base_url = "https://portdan.com/v1/responses"\n'
            'model_providers.customer_alias.apiKey = "{}"\n'.format(KEY)
        )
        with patch.object(generate_image, "tomllib", None):
            parsed = generate_image._parse_config(config.encode("utf-8"))
        request_config = generate_image._request_config_from_config(parsed, None, {})
        self.assertIsNotNone(request_config)
        assert request_config is not None
        self.assertEqual(request_config.api_key, KEY)

    def test_python39_fallback_supports_quoted_dotted_provider_key_aliases(self) -> None:
        config = (
            'model_provider = "customer alias"\n'
            'model_providers."customer alias".base_url = "https://portdan.com/v1"\n'
            'model_providers."customer alias".apiKey = "{}"\n'.format(KEY)
        )
        with patch.object(generate_image, "tomllib", None):
            parsed = generate_image._parse_config(config.encode("utf-8"))
        request_config = generate_image._request_config_from_config(parsed, None, {})
        self.assertIsNotNone(request_config)
        assert request_config is not None
        self.assertEqual(request_config.api_key, KEY)

    def test_missing_key_returns_one_actionable_message_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(Path, "home", return_value=home), patch.dict(
                os.environ, {}, clear=True
            ), patch.object(generate_image, "_post") as post, patch.object(
                Path, "cwd", return_value=home
            ), patch.object(
                sys, "stdin", io.StringIO("a dog")
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = generate_image.main(["--prompt-stdin", "--quality", "low"])
            self.assertEqual(code, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue().strip(), generate_image.MISSING_KEY_MESSAGE)
            self.assertIn("没有发送", stderr.getvalue())
            self.assertFalse((home / "portdan-images").exists())
            post.assert_not_called()

    def test_api_key_stdin_overrides_automatic_key_for_one_process_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_cc_database(
                home,
                'openai_base_url = "https://portdan.com/v1"\n',
                {"OPENAI_API_KEY": OTHER_KEY},
            )
            stdout, stderr = io.StringIO(), io.StringIO()
            observed: list[tuple[str, str | None]] = []
            request_configs: list[generate_image.RequestConfig] = []
            original_payload = generate_image._payload

            def payload(
                request_config: generate_image.RequestConfig,
                prompt: str,
                size: str,
                quality: str,
                count: int = 1,
            ) -> bytes:
                request_configs.append(request_config)
                return original_payload(request_config, prompt, size, quality, count)

            def post(api_key: str, _body: bytes, _timeout: float, **_kwargs) -> bytes:
                observed.append((api_key, os.environ.get("PORTDAN_API_KEY")))
                return response_body()

            with patch.object(Path, "home", return_value=home), patch.object(
                Path, "cwd", return_value=home
            ), patch.dict(
                os.environ, {"PORTDAN_API_KEY": OTHER_KEY}, clear=True
            ), patch.object(
                generate_image,
                "resolve_request_config",
                return_value=generate_image.RequestConfig(
                    api_key=OTHER_KEY,
                    model=MODEL,
                    source=generate_image.KEY_SOURCE_CC_SWITCH,
                ),
            ) as resolve, patch.object(
                generate_image, "_payload", side_effect=payload
            ), patch.object(
                generate_image, "_post", side_effect=post
            ) as post_mock, patch.object(
                sys, "stdin", io.StringIO("a dog\n{}\n".format(KEY))
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = generate_image.main([
                    "--api-key-stdin", "--size", "1024x1024", "--quality", "low"
                ])
                restored_value = os.environ.get("PORTDAN_API_KEY")

            self.assertEqual(code, 0)
            resolve.assert_not_called()
            post_mock.assert_called_once()
            self.assertEqual(observed, [(KEY, OTHER_KEY)])
            self.assertEqual(restored_value, OTHER_KEY)
            self.assertEqual(len(request_configs), 1)
            self.assertEqual(request_configs[0].api_key, KEY)
            self.assertEqual(
                request_configs[0].model,
                generate_image.DEFAULT_RESPONSE_MODEL,
            )
            self.assertEqual(request_configs[0].source, generate_image.KEY_SOURCE_STDIN)
            self.assertNotIn(KEY, repr(request_configs[0]))
            combined_output = stdout.getvalue() + stderr.getvalue()
            self.assertNotIn(KEY, combined_output)
            self.assertNotIn(OTHER_KEY, combined_output)
            self.assertIn("本次提供", stderr.getvalue())
            output = Path(stdout.getvalue().strip())
            self.assertEqual(output.read_bytes(), PNG)

    def test_api_key_stdin_never_exports_key_to_environment(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        failure = generate_image.RequestError(503, kind="http", response_started=True)
        with patch.dict(os.environ, {}, clear=True), patch.object(
            generate_image, "_post", side_effect=failure
        ) as post, patch.object(
            sys, "stdin", io.StringIO("a dog\n{}\n".format(KEY))
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = generate_image.main(["--api-key-stdin", "--quality", "medium"])
            key_remains = "PORTDAN_API_KEY" in os.environ
        self.assertEqual(code, 4)
        post.assert_called_once()
        self.assertFalse(key_remains)
        self.assertNotIn(KEY, stdout.getvalue() + stderr.getvalue())

    def test_api_key_stdin_rejects_tty_missing_empty_and_invalid_key_without_request(self) -> None:
        cases = (
            ("tty", TtyInput("a dog\n{}\n".format(KEY))),
            ("unverifiable-tty", UnverifiableTtyInput("a dog\n{}\n".format(KEY))),
            ("missing", io.StringIO("a dog\n")),
            ("empty", io.StringIO("a dog\n\n")),
            ("invalid", io.StringIO("a dog\n{} invalid\n".format(KEY))),
        )
        for label, stdin in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.dict(
                    os.environ, {"PORTDAN_API_KEY": OTHER_KEY}, clear=True
                ), patch.object(
                    Path, "cwd", return_value=directory
                ), patch.object(
                    generate_image, "resolve_request_config"
                ) as resolve, patch.object(
                    generate_image, "_post"
                ) as post, patch.object(
                    sys, "stdin", stdin
                ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = generate_image.main(["--api-key-stdin", "--quality", "low"])
                    restored_value = os.environ.get("PORTDAN_API_KEY")
                self.assertEqual(code, 2)
                resolve.assert_not_called()
                post.assert_not_called()
                self.assertEqual(restored_value, OTHER_KEY)
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn("没有发送", stderr.getvalue())
                self.assertNotIn(KEY, stderr.getvalue())
                self.assertNotIn(OTHER_KEY, stderr.getvalue())
                self.assertFalse((directory / "portdan-images").exists())

    def test_api_key_stdin_does_not_modify_config_profiles_or_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            protected = (
                home / ".codex" / "config.toml",
                home / ".codex" / "auth.json",
                home / ".cc-switch" / "settings.json",
                home / ".zshrc",
                home / ".bash_profile",
                home / ".config" / "powershell" / "Microsoft.PowerShell_profile.ps1",
                home / "runner.log",
            )
            for index, path in enumerate(protected):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("sentinel-{}\n".format(index), encoding="utf-8")
            before_hashes = {
                path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected
            }
            before_files = {path.relative_to(home) for path in home.rglob("*") if path.is_file()}
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(Path, "home", return_value=home), patch.object(
                Path, "cwd", return_value=home
            ), patch.dict(os.environ, {}, clear=True), patch.object(
                generate_image, "_post", return_value=response_body()
            ) as post, patch.object(
                sys, "stdin", io.StringIO("a dog\n{}\n".format(KEY))
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = generate_image.main(["--api-key-stdin", "--quality", "low"])
                key_remains = "PORTDAN_API_KEY" in os.environ

            self.assertEqual(code, 0)
            post.assert_called_once()
            self.assertFalse(key_remains)
            for path, before_hash in before_hashes.items():
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before_hash)
            new_files = {
                path.relative_to(home) for path in home.rglob("*") if path.is_file()
            } - before_files
            self.assertEqual(len(new_files), 1)
            self.assertEqual(next(iter(new_files)).parts[0], "portdan-images")
            for path in home.rglob("*"):
                if path.is_file():
                    self.assertNotIn(KEY.encode(), path.read_bytes())
            self.assertNotIn(KEY, stdout.getvalue() + stderr.getvalue())

    def test_prompt_payload_is_minimal_and_legacy_flags_are_unrestricted(self) -> None:
        self.assertEqual(
            generate_image.ENDPOINT,
            "https://portdan.com/v1/images/generations",
        )
        request_config = generate_image.RequestConfig(api_key=KEY, model=MODEL)
        payload = json.loads(generate_image._payload(request_config, "a dog"))
        self.assertEqual(payload, {
            "prompt": "a dog",
            "response_format": "b64_json",
            "stream": True,
        })
        payload = json.loads(
            generate_image._payload(
                request_config, "many dogs", "future-size", "future-quality", 40,
                model="gpt-image-9.7-preview",
            )
        )
        self.assertEqual(payload["n"], 40)
        self.assertEqual(payload["size"], "future-size")
        self.assertEqual(payload["quality"], "future-quality")
        self.assertEqual(payload["model"], "gpt-image-9.7-preview")

    def test_legacy_count_values_are_forwarded_for_portdan_validation(self) -> None:
        for count in (0, -1):
            with self.subTest(count=count), patch.object(
                generate_image,
                "resolve_request_config",
                return_value=generate_image.RequestConfig(api_key=KEY),
            ), patch.object(
                generate_image, "_post", side_effect=generate_image.RequestError(400)
            ) as post, patch.object(
                sys, "stdin", io.StringIO("a dog")
            ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                code = generate_image.main([
                    "--prompt-stdin", "--quality", "low", "--count", str(count)
                ])
            self.assertEqual(code, 4)
            post.assert_called_once()
            self.assertEqual(json.loads(post.call_args.args[1])["n"], count)
            self.assertIsNone(post.call_args.kwargs["expected_count"])

    def test_count_has_no_client_side_maximum(self) -> None:
        args = generate_image._parser().parse_args(["--prompt-stdin", "--count", "500"])
        self.assertEqual(args.count, 500)

    def test_multi_image_stream_posts_once_and_publishes_in_arrival_order(self) -> None:
        images = (colored_png(255, 0, 0), colored_png(0, 255, 0))
        events = "".join(
            "event: image_generation.completed\n"
            "data: {\"type\":\"image_generation.completed\",\"b64_json\":\""
            + base64.b64encode(image).decode("ascii")
            + "\"}\n\n"
            for image in images
        ) + "data: [DONE]\n\n"
        opener = Mock()
        opener.open.return_value = HTTPBodyResponse(
            events.encode("ascii"),
            content_type="text/event-stream",
            request_id="pdi-batch-server",
        )
        with tempfile.TemporaryDirectory() as temp:
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(
                generate_image,
                "resolve_request_config",
                return_value=generate_image.RequestConfig(api_key=KEY, model=MODEL),
            ), patch.object(
                generate_image, "build_opener", return_value=opener
            ), patch.object(
                generate_image, "_new_request_id", return_value="pdi-batch-client"
            ), patch.object(
                Path, "cwd", return_value=Path(temp)
            ), patch.object(
                sys, "stdin", io.StringIO("two dogs")
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = generate_image.main([
                    "--prompt-stdin", "--quality", "low", "--count", "2"
                ])

            self.assertEqual(code, 0)
            paths = [Path(line) for line in stdout.getvalue().splitlines()]
            self.assertEqual(len(paths), 2)
            self.assertEqual([path.read_bytes() for path in paths], list(images))
            self.assertEqual(paths[0].name, "image-1.png")
            self.assertEqual(paths[1].name, "image-2.png")
            self.assertEqual(paths[0].parent, paths[1].parent)
            self.assertIn("2 个 artifact", stderr.getvalue())
            self.assertIn("pdi-batch-server", stderr.getvalue())

        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertEqual(json.loads(request.data)["n"], 2)
        self.assertEqual(request.get_header("X-request-id"), "pdi-batch-client")

    def test_json_data_array_is_staged_one_image_at_a_time_and_published(self) -> None:
        images = (colored_png(1, 2, 3), colored_png(4, 5, 6))
        body = json.dumps({
            "data": [
                {"b64_json": base64.b64encode(image).decode("ascii")}
                for image in images
            ]
        }).encode("ascii")
        opener = Mock()
        opener.open.return_value = HTTPBodyResponse(body, request_id="pdi-json-batch")
        with tempfile.TemporaryDirectory() as temp:
            stdout = io.StringIO()
            with patch.object(
                generate_image,
                "resolve_request_config",
                return_value=generate_image.RequestConfig(api_key=KEY, model=MODEL),
            ), patch.object(
                generate_image, "build_opener", return_value=opener
            ), patch.object(
                Path, "cwd", return_value=Path(temp)
            ), patch.object(
                sys, "stdin", io.StringIO("two colors")
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                code = generate_image.main([
                    "--prompt-stdin", "--quality", "medium", "--count", "2"
                ])
            self.assertEqual(code, 0)
            paths = [Path(line) for line in stdout.getvalue().splitlines()]
            self.assertEqual([path.read_bytes() for path in paths], list(images))
        opener.open.assert_called_once()

    def test_partial_batch_publishes_all_complete_pngs_with_dedicated_exit(self) -> None:
        images = (colored_png(7, 8, 9), colored_png(10, 11, 12))
        stream = (
            "".join(
                "event: image_generation.completed\n"
                "data: {\"type\":\"image_generation.completed\",\"b64_json\":\""
                + base64.b64encode(image).decode("ascii")
                + "\"}\n\n"
                for image in images
            )
            + "data: [DONE]\n\n"
        ).encode("ascii")
        opener = Mock()
        opener.open.return_value = HTTPBodyResponse(
            stream, content_type="text/event-stream", request_id="pdi-partial"
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(
                generate_image,
                "resolve_request_config",
                return_value=generate_image.RequestConfig(api_key=KEY, model=MODEL),
            ), patch.object(
                generate_image, "build_opener", return_value=opener
            ), patch.object(
                Path, "cwd", return_value=root
            ), patch.object(
                sys, "stdin", io.StringIO("two dogs")
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = generate_image.main([
                    "--prompt-stdin", "--quality", "low", "--count", "3"
                ])
            self.assertEqual(code, 7)
            paths = [Path(line) for line in stdout.getvalue().splitlines()]
            self.assertEqual(len(paths), 2)
            self.assertEqual([path.read_bytes() for path in paths], list(images))
            self.assertTrue(all(path.read_bytes().startswith(b"\x89PNG") for path in paths))
            self.assertIn("2/3", stderr.getvalue())
            self.assertIn("已安全发布", stderr.getvalue())
            self.assertIn("pdi-partial", stderr.getvalue())
            batches = list((root / "portdan-images").iterdir())
            self.assertEqual(len(batches), 1)
            self.assertEqual(batches[0].resolve(), paths[0].parent.resolve())
            self.assertEqual(
                sorted(path.resolve() for path in batches[0].iterdir()),
                sorted(path.resolve() for path in paths),
            )
        opener.open.assert_called_once()

    def test_partial_stream_done_eof_and_error_are_all_partial(self) -> None:
        encoded = base64.b64encode(PNG).decode("ascii")
        completed = (
            "event: image_generation.completed\n"
            "data: {\"type\":\"image_generation.completed\",\"b64_json\":\""
            + encoded
            + "\"}\n\n"
        )
        for tail in ("data: [DONE]\n\n", "", "event: error\n\n"):
            with self.subTest(tail=tail), self.assertRaises(
                generate_image.PartialImageError
            ) as raised:
                generate_image._read_image_stream(
                    HTTPBodyResponse(
                        (completed + tail).encode("ascii"),
                        content_type="text/event-stream",
                    ),
                    started=time.monotonic(),
                    overall_timeout=900,
                    idle_timeout=60,
                    request_id="pdi-partial-terminal",
                    expected_count=2,
                    on_image=lambda _payload: None,
                )
            self.assertEqual(raised.exception.completed, 1)
            self.assertEqual(raised.exception.expected, 2)

    def test_partial_overall_or_transport_after_completed_is_published_once(self) -> None:
        encoded = base64.b64encode(PNG).decode("ascii")
        completed_lines = (
            b"event: image_generation.completed\n",
            (
                "data: {\"type\":\"image_generation.completed\",\"b64_json\":\""
                + encoded
                + "\"}\n"
            ).encode("ascii"),
            b"\n",
        )

        class TransportAfterCompleted:
            def __init__(self) -> None:
                self.lines = iter(completed_lines)

            def readline(self, _limit=-1):
                try:
                    return next(self.lines)
                except StopIteration:
                    raise ConnectionResetError("stream reset after completed image")

        def overall_post(_key, _body, _timeout, **kwargs):
            response = HTTPBodyResponse(b"".join(completed_lines), content_type="text/event-stream")
            with patch.object(
                generate_image.time,
                "monotonic",
                side_effect=[0.0, 0.0, 0.0, 0.0, 2.0, 2.0],
            ):
                return generate_image._read_image_stream(
                    response,
                    started=0.0,
                    overall_timeout=1.0,
                    idle_timeout=60.0,
                    request_id="pdi-partial-overall",
                    expected_count=2,
                    on_image=kwargs["on_image"],
                )

        def transport_post(_key, _body, _timeout, **kwargs):
            return generate_image._read_image_stream(
                TransportAfterCompleted(),
                started=time.monotonic(),
                overall_timeout=900.0,
                idle_timeout=60.0,
                request_id="pdi-partial-transport",
                expected_count=2,
                on_image=kwargs["on_image"],
            )

        for name, side_effect in (
            ("overall", overall_post),
            ("transport", transport_post),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                post = Mock(side_effect=side_effect)
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.object(
                    generate_image,
                    "resolve_request_config",
                    return_value=generate_image.RequestConfig(api_key=KEY, model=MODEL),
                ), patch.object(
                    generate_image, "_post", post
                ), patch.object(
                    Path, "cwd", return_value=Path(temp)
                ), patch.object(
                    sys, "stdin", io.StringIO("two dogs")
                ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = generate_image.main([
                        "--prompt-stdin", "--quality", "low", "--count", "2"
                    ])
                self.assertEqual(code, 7)
                paths = [Path(line) for line in stdout.getvalue().splitlines()]
                self.assertEqual(len(paths), 1)
                self.assertEqual(paths[0].read_bytes(), PNG)
                self.assertIn("1/2", stderr.getvalue())
                post.assert_called_once()

    def test_excess_or_malformed_completed_event_publishes_nothing(self) -> None:
        valid = base64.b64encode(PNG).decode("ascii")
        scenarios = {
            "excess": [valid, valid, valid],
            "malformed": [valid, "not-valid-base64"],
        }
        for name, values in scenarios.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                stream = "".join(
                    "event: image_generation.completed\n"
                    "data: {\"type\":\"image_generation.completed\",\"b64_json\":\""
                    + value
                    + "\"}\n\n"
                    for value in values
                ) + "data: [DONE]\n\n"
                opener = Mock()
                opener.open.return_value = HTTPBodyResponse(
                    stream.encode("ascii"), content_type="text/event-stream"
                )
                root = Path(temp)
                with patch.object(
                    generate_image,
                    "resolve_request_config",
                    return_value=generate_image.RequestConfig(api_key=KEY, model=MODEL),
                ), patch.object(
                    generate_image, "build_opener", return_value=opener
                ), patch.object(
                    Path, "cwd", return_value=root
                ), patch.object(
                    sys, "stdin", io.StringIO("two dogs")
                ), contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(
                    io.StringIO()
                ):
                    code = generate_image.main([
                        "--prompt-stdin", "--quality", "low", "--count", "2"
                    ])
                self.assertEqual(code, 5)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(list((root / "portdan-images").iterdir()), [])
                opener.open.assert_called_once()

    def test_main_posts_once_writes_png_and_reports_model_and_elapsed_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            stdout, stderr = io.StringIO(), io.StringIO()
            request = Mock(return_value=response_body())
            request_config = generate_image.RequestConfig(api_key=KEY, model=MODEL)
            with patch.object(
                generate_image, "resolve_request_config", return_value=request_config
            ), patch.object(generate_image, "_post", request), patch.object(
                generate_image, "_new_request_id", return_value="pdi-main-safe"
            ), patch.object(
                Path, "cwd", return_value=directory
            ), patch.object(
                sys, "stdin", io.StringIO("a dog")
            ), patch.object(
                generate_image.time, "monotonic", side_effect=[10.0, 12.25]
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = generate_image.main([
                    "--prompt-stdin", "--size", "1024x1024", "--quality", "low"
                ])
            self.assertEqual(code, 0)
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.args[0], KEY)
            payload = json.loads(request.call_args.args[1])
            self.assertNotIn("model", payload)
            self.assertEqual(payload["quality"], "low")
            self.assertTrue(payload["stream"])
            self.assertNotIn("tools", payload)
            output = Path(stdout.getvalue().strip())
            self.assertTrue(output.is_absolute())
            self.assertEqual(output.read_bytes(), PNG)
            self.assertIn("GPT Images", stderr.getvalue())
            self.assertIn("耗时 2.2 秒", stderr.getvalue())
            self.assertIn("请求 ID：pdi-main-safe", stderr.getvalue())

    def test_cli_defaults_to_direct_proxy_without_overall_deadline(self) -> None:
        args = generate_image._parser().parse_args(["--prompt-stdin"])
        self.assertEqual(args.proxy_mode, "direct")
        self.assertIsNone(args.timeout)
        self.assertEqual(generate_image.IDLE_TIMEOUT_SECONDS, 1800.0)

    def test_cli_rejects_non_finite_timeout_before_key_or_network(self) -> None:
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value), patch.object(
                generate_image,
                "resolve_request_config",
            ) as resolve, patch.object(generate_image, "_post") as post, patch.object(
                sys,
                "stdin",
                io.StringIO("a dog"),
            ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = generate_image.main(["--prompt-stdin", "--timeout=" + value])
            self.assertEqual(code, 2)
            resolve.assert_not_called()
            post.assert_not_called()

    def test_existing_output_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            with patch.object(Path, "cwd", return_value=directory):
                first = generate_image._save_png(PNG)
                second = generate_image._save_png(PNG)
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_bytes(), PNG)

    def test_output_directory_rename_failure_keeps_a_complete_recovery_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.object(Path, "cwd", return_value=Path(temp)):
            original_rename = generate_image.os.rename

            def fail_publication(source, destination):
                if Path(source).name.startswith(".portdan-image2-stage-"):
                    raise OSError("unsupported")
                return original_rename(source, destination)

            with patch.object(generate_image.os, "rename", side_effect=fail_publication):
                with self.assertRaises(generate_image.OutputRecoveryError) as raised:
                    generate_image._save_png(PNG)
            recovery = raised.exception.path
            self.assertTrue(recovery.is_dir())
            self.assertEqual(next(recovery.iterdir()).read_bytes(), PNG)

    def test_png_bytes_are_not_semantically_rewritten(self) -> None:
        iend = PNG.rfind(b"IEND") - 4
        decorated = PNG[:iend] + png_chunk(b"tEXt", b"prompt=private") + PNG[iend:]
        cleaned = generate_image._sanitize_png(decorated)
        self.assertIn(b"tEXt", cleaned)
        self.assertEqual(cleaned, decorated)
        self.assertEqual(generate_image._sanitize_png(cleaned), cleaned)

    def test_responses_image_generation_result_is_supported(self) -> None:
        raw = json.dumps({
            "output": [{
                "type": "message",
                "content": [],
            }, {
                "type": "image_generation_call",
                "result": "data:image/png;base64," + base64.b64encode(PNG).decode("ascii"),
            }]
        }).encode()
        self.assertEqual(generate_image._image_bytes(raw), PNG)

    def test_invalid_responses_image_output_is_reported(self) -> None:
        for raw in (
            b"{}",
            b'{"data":[]}',
            b'{"output":[]}',
            b'{"output":[{"type":"image_generation_call"}]}',
            b'{"output":[{"type":"image_generation_call","result":"a"},{"type":"image_generation_call","result":"b"}]}',
        ):
            with self.subTest(raw=raw), self.assertRaises(generate_image.ResponseError):
                generate_image._image_bytes(raw)

    def test_symlinked_output_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target"
            target.mkdir()
            output = root / "portdan-images"
            try:
                os.symlink(target, output, target_is_directory=True)
            except (NotImplementedError, OSError) as exc:
                self.skipTest("symlink creation is unavailable: {}".format(exc))
            with patch.object(Path, "cwd", return_value=root):
                with self.assertRaises(OSError):
                    generate_image._save_png(PNG)
            self.assertEqual(list(target.iterdir()), [])

    def test_proxy_environment_is_disabled_for_authenticated_request(self) -> None:
        opener = Mock()
        opener.open.side_effect = generate_image.URLError("offline")
        with patch.object(generate_image, "build_opener", return_value=opener) as build:
            with self.assertRaises(generate_image.RequestError):
                generate_image._post(KEY, b"{}", 1)
        proxy = next(
            item for item in build.call_args.args if isinstance(item, generate_image.ProxyHandler)
        )
        self.assertEqual(proxy.proxies, {})

    def test_system_proxy_mode_uses_host_proxy_configuration(self) -> None:
        opener = Mock()
        opener.open.return_value = HTTPBodyResponse(b"{}")
        with patch.dict(
            os.environ,
            {"HTTPS_PROXY": "http://127.0.0.1:7897"},
            clear=True,
        ), patch.object(generate_image, "build_opener", return_value=opener) as build:
            generate_image._post(KEY, b"{}", 30, proxy_mode="system")
        proxy = next(
            item for item in build.call_args.args if isinstance(item, generate_image.ProxyHandler)
        )
        self.assertEqual(proxy.proxies.get("https"), "http://127.0.0.1:7897")

    def test_stream_keeps_only_completed_image_and_ignores_keepalive_and_done(self) -> None:
        encoded = base64.b64encode(PNG).decode("ascii")
        stream = (
            ":\n\n"
            "event: image_generation.partial_image\n"
            "data: {\"type\":\"image_generation.partial_image\",\"b64_json\":\"cGFydGlhbA==\"}\n\n"
            "event: image_generation.completed\n"
            "data: {\"type\":\"image_generation.completed\",\"b64_json\":\""
            + encoded
            + "\"}\n\n"
            "data: [DONE]\n\n"
        ).encode("ascii")
        opener = Mock()
        opener.open.return_value = HTTPBodyResponse(
            stream,
            content_type="text/event-stream",
            request_id="pdi-server-safe-123",
        )
        with patch.object(generate_image, "build_opener", return_value=opener):
            result = generate_image._post(
                KEY,
                b"{}",
                900,
                heartbeat_interval=0,
                client_request_id="pdi-client-safe-123",
            )
        self.assertEqual(result.request_id, "pdi-server-safe-123")
        self.assertEqual(generate_image._image_bytes(result.body), PNG)
        self.assertNotIn(b"cGFydGlhbA==", result.body)
        opener.open.assert_called_once()

    def test_stream_supports_multiline_completed_event(self) -> None:
        encoded = base64.b64encode(PNG).decode("ascii")
        stream = (
            "event:image_generation.completed\n"
            "data: {\"type\":\"image_generation.completed\",\n"
            "data: \"b64_json\":\""
            + encoded
            + "\"}\n\n"
            "data:[DONE]\n\n"
        ).encode("ascii")
        result, first_event = generate_image._read_image_stream(
            HTTPBodyResponse(stream, content_type="text/event-stream"),
            started=time.monotonic(),
            overall_timeout=900,
            idle_timeout=60,
            request_id="pdi-test",
        )
        self.assertEqual(generate_image._image_bytes(result), PNG)
        self.assertIsNotNone(first_event)

    def test_multiline_sse_event_has_its_own_payload_limit(self) -> None:
        stream = (
            b"event: image_generation.completed\n"
            b"data: {\"type\":\"image_\n"
            b"data: generation.complet\n"
            b"data: ed\",\"b64_json\":\"a\"}\n\n"
        )
        opener = Mock()
        opener.open.return_value = HTTPBodyResponse(
            stream, content_type="text/event-stream", request_id="pdi-event-limit"
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(
                generate_image,
                "resolve_request_config",
                return_value=generate_image.RequestConfig(api_key=KEY, model=MODEL),
            ), patch.object(
                generate_image, "build_opener", return_value=opener
            ), patch.object(
                Path, "cwd", return_value=root
            ), patch.object(
                sys, "stdin", io.StringIO("four dogs")
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = generate_image.main([
                    "--prompt-stdin", "--quality", "low", "--count", "4",
                    "--json-memory-limit-mib", "0.00003814697265625",
                ])
            self.assertEqual(code, 6)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("本机资源保护", stderr.getvalue())
            self.assertEqual(list((root / "portdan-images").iterdir()), [])
        opener.open.assert_called_once()

    def test_done_returns_immediately_after_completed_without_waiting_for_eof(self) -> None:
        encoded = base64.b64encode(PNG).decode("ascii")
        stream = (
            "event: image_generation.completed\n"
            "data: {\"type\":\"image_generation.completed\",\"b64_json\":\""
            + encoded
            + "\"}\n\n"
            "data: [DONE]\n\n"
        ).encode("ascii")

        class NoReadAfterDone(HTTPBodyResponse):
            reads = 0

            def readline(self, limit=-1):
                self.reads += 1
                if self.reads > 5:
                    raise AssertionError("reader waited for EOF after [DONE]")
                return super().readline(limit)

        response = NoReadAfterDone(stream, content_type="text/event-stream")
        result, _ = generate_image._read_image_stream(
            response,
            started=time.monotonic(),
            overall_timeout=900,
            idle_timeout=60,
            request_id="pdi-done",
        )
        self.assertEqual(generate_image._image_bytes(result), PNG)
        self.assertEqual(response.reads, 5)

    def test_done_without_completed_is_truncated_immediately(self) -> None:
        response = HTTPBodyResponse(
            b"data: [DONE]\n\nignored-after-done",
            content_type="text/event-stream",
        )
        with self.assertRaises(generate_image.RequestError) as raised:
            generate_image._read_image_stream(
                response,
                started=time.monotonic(),
                overall_timeout=900,
                idle_timeout=60,
                request_id="pdi-done-without-final",
            )
        self.assertEqual(raised.exception.kind, "truncated_stream")
        self.assertEqual(raised.exception.request_id, "pdi-done-without-final")

    def test_stream_json_fallback_uses_images_response_shape(self) -> None:
        encoded = base64.b64encode(PNG).decode("ascii")
        raw = json.dumps({"data": [{"b64_json": encoded}]}).encode()
        opener = Mock()
        opener.open.return_value = HTTPBodyResponse(
            raw,
            content_type="text/event-stream",
        )
        with patch.object(generate_image, "build_opener", return_value=opener):
            result = generate_image._post(KEY, b"{}", 900, heartbeat_interval=0)
        self.assertEqual(generate_image._image_bytes(result.body), PNG)

    def test_sse_content_type_json_fallback_cap_does_not_grow_with_count(self) -> None:
        prefix = b'{\n"data":[{"b64_json":"'
        suffix = b'"}]}'
        raw = prefix + (b"A" * (443 - len(prefix) - len(suffix))) + suffix
        self.assertEqual(len(raw), 443)
        opener = Mock()
        opener.open.return_value = HTTPBodyResponse(
            raw,
            content_type="text/event-stream",
            request_id="pdi-sse-json-cap",
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(
                generate_image,
                "resolve_request_config",
                return_value=generate_image.RequestConfig(api_key=KEY, model=MODEL),
            ), patch.object(
                generate_image, "build_opener", return_value=opener
            ), patch.object(
                Path, "cwd", return_value=root
            ), patch.object(
                sys, "stdin", io.StringIO("four dogs")
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = generate_image.main([
                    "--prompt-stdin", "--quality", "low", "--count", "4",
                    "--json-memory-limit-mib", "0.0001220703125",
                ])
            self.assertEqual(code, 6)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("本机资源保护", stderr.getvalue())
            self.assertEqual(list((root / "portdan-images").iterdir()), [])
        opener.open.assert_called_once()

    def test_json_fallback_raw_cap_does_not_grow_with_count(self) -> None:
        opener = Mock()
        opener.open.return_value = HTTPBodyResponse(
            b"12345678901", content_type="application/json"
        )
        with patch.object(
            generate_image, "build_opener", return_value=opener
        ), self.assertRaises(generate_image.LocalResourceError) as raised:
            generate_image._post(
                KEY,
                b"{}",
                900,
                expected_count=4,
                heartbeat_interval=0,
                json_memory_limit=10,
            )
        self.assertEqual(raised.exception.code, "json_memory_limit")
        opener.open.assert_called_once()

    def test_json_array_decodes_and_stages_each_value_without_json_roundtrip(self) -> None:
        images = (colored_png(20, 21, 22), colored_png(23, 24, 25))
        raw = json.dumps({
            "data": [
                {"b64_json": base64.b64encode(image).decode("ascii")}
                for image in images
            ]
        }).encode("ascii")
        original_decode = generate_image._decode_image_value
        with tempfile.TemporaryDirectory() as temp, patch.object(
            Path, "cwd", return_value=Path(temp)
        ):
            with generate_image._ImageBatch(2) as batch, patch.object(
                generate_image,
                "_decode_image_value",
                wraps=original_decode,
            ) as decode, patch.object(
                generate_image,
                "_image_bytes",
                side_effect=AssertionError("must not parse a rebuilt per-image JSON payload"),
            ), patch.object(
                generate_image.json,
                "dumps",
                side_effect=AssertionError("must not rebuild per-image JSON"),
            ):
                count = generate_image._consume_image_response(
                    raw,
                    expected_count=2,
                    on_image=batch.stage_payload,
                )
                paths = batch.publish()
            self.assertEqual(count, 2)
            self.assertEqual(decode.call_count, 0)
            self.assertEqual([path.read_bytes() for path in paths], list(images))

    def test_stream_json_fallback_rechecks_overall_after_remainder_read(self) -> None:
        response = HTTPBodyResponse(
            b'{"data":[]}',
            content_type="text/event-stream",
        )
        with patch.object(
            generate_image.time,
            "monotonic",
            side_effect=[55.0, 61.0],
        ), self.assertRaises(generate_image.RequestError) as raised:
            generate_image._read_image_stream(
                response,
                started=0.0,
                overall_timeout=60.0,
                idle_timeout=60.0,
                request_id="pdi-json-overall",
            )
        self.assertEqual(raised.exception.kind, "overall_timeout")
        self.assertEqual(raised.exception.elapsed, 61.0)
        self.assertEqual(raised.exception.request_id, "pdi-json-overall")

    def test_truncated_stream_is_not_treated_as_success(self) -> None:
        response = HTTPBodyResponse(
            b"event: image_generation.partial_image\n"
            b"data: {\"type\":\"image_generation.partial_image\",\"b64_json\":\"abc\"}\n\n",
            content_type="text/event-stream",
        )
        with self.assertRaises(generate_image.RequestError) as raised:
            generate_image._read_image_stream(
                response,
                started=time.monotonic(),
                overall_timeout=900,
                idle_timeout=60,
                request_id="pdi-truncated",
            )
        self.assertEqual(raised.exception.kind, "truncated_stream")
        self.assertEqual(raised.exception.request_id, "pdi-truncated")

    def test_stream_error_event_stops_without_retry_or_partial_save(self) -> None:
        response = HTTPBodyResponse(
            b"event: error\n"
            b"data: {\"type\":\"error\",\"error\":{\"message\":\"upstream failed\"}}\n\n",
            content_type="text/event-stream",
        )
        with self.assertRaises(generate_image.RequestError) as raised:
            generate_image._read_image_stream(
                response,
                started=time.monotonic(),
                overall_timeout=900,
                idle_timeout=60,
                request_id="pdi-stream-error",
            )
        self.assertEqual(raised.exception.kind, "stream_error")
        self.assertEqual(raised.exception.request_id, "pdi-stream-error")

    def test_stream_error_event_with_missing_or_empty_data_is_immediate_error(self) -> None:
        for body in (
            b"event: error\n\n",
            b"event: error\ndata:\n\n",
        ):
            with self.subTest(body=body), self.assertRaises(
                generate_image.RequestError
            ) as raised:
                generate_image._read_image_stream(
                    HTTPBodyResponse(body, content_type="text/event-stream"),
                    started=time.monotonic(),
                    overall_timeout=900,
                    idle_timeout=60,
                    request_id="pdi-empty-error",
                )
            self.assertEqual(raised.exception.kind, "stream_error")
            self.assertEqual(raised.exception.request_id, "pdi-empty-error")

    def test_empty_stream_error_event_uses_one_post_and_stops(self) -> None:
        opener = Mock()
        opener.open.return_value = HTTPBodyResponse(
            b"event: error\n\n",
            content_type="text/event-stream",
            request_id="pdi-empty-error-post",
        )
        with patch.object(
            generate_image,
            "build_opener",
            return_value=opener,
        ), self.assertRaises(generate_image.RequestError) as raised:
            generate_image._post(KEY, b"{}", 900, heartbeat_interval=0)
        self.assertEqual(raised.exception.kind, "stream_error")
        self.assertEqual(raised.exception.request_id, "pdi-empty-error-post")
        opener.open.assert_called_once()

    def test_stream_enforces_overall_deadline_before_read(self) -> None:
        with patch.object(generate_image.time, "monotonic", return_value=901.0):
            with self.assertRaises(generate_image.RequestError) as raised:
                generate_image._read_image_stream(
                    HTTPBodyResponse(b""),
                    started=0.0,
                    overall_timeout=900,
                    idle_timeout=60,
                    request_id="pdi-overall-timeout",
                )
        self.assertEqual(raised.exception.kind, "overall_timeout")
        self.assertEqual(raised.exception.stage, "stream")

    def test_remaining_driven_read_timeout_is_overall_for_sse_and_json(self) -> None:
        class TimeoutResponse(HTTPBodyResponse):
            def readline(self, _limit=-1):
                raise socket.timeout("remaining deadline elapsed")

            def read(self, _limit=-1):
                raise socket.timeout("remaining deadline elapsed")

        readers = (
            lambda response: generate_image._read_image_stream(
                response,
                started=0.0,
                overall_timeout=60.0,
                idle_timeout=60.0,
                request_id="pdi-remaining-sse",
            ),
            lambda response: generate_image._read_bounded_json_response(
                response,
                started=0.0,
                overall_timeout=60.0,
                idle_timeout=60.0,
                request_id="pdi-remaining-json",
            ),
        )
        for reader in readers:
            with self.subTest(reader=reader), patch.object(
                generate_image.time,
                "monotonic",
                side_effect=[55.0, 60.0],
            ), self.assertRaises(generate_image.RequestError) as raised:
                reader(TimeoutResponse(b""))
            self.assertEqual(raised.exception.kind, "overall_timeout")
            self.assertEqual(raised.exception.stage, "stream")
            self.assertEqual(raised.exception.elapsed, 60.0)
            self.assertTrue((raised.exception.request_id or "").startswith("pdi-remaining-"))

    def test_genuine_idle_read_timeout_is_idle_for_sse_and_json(self) -> None:
        class TimeoutResponse(HTTPBodyResponse):
            def readline(self, _limit=-1):
                raise socket.timeout("idle deadline elapsed")

            def read(self, _limit=-1):
                raise socket.timeout("idle deadline elapsed")

        readers = (
            lambda response: generate_image._read_image_stream(
                response,
                started=0.0,
                overall_timeout=900.0,
                idle_timeout=60.0,
                request_id="pdi-idle-sse",
            ),
            lambda response: generate_image._read_bounded_json_response(
                response,
                started=0.0,
                overall_timeout=900.0,
                idle_timeout=60.0,
                request_id="pdi-idle-json",
            ),
        )
        for reader in readers:
            with self.subTest(reader=reader), patch.object(
                generate_image.time,
                "monotonic",
                side_effect=[100.0, 160.0],
            ), self.assertRaises(generate_image.RequestError) as raised:
                reader(TimeoutResponse(b""))
            self.assertEqual(raised.exception.kind, "idle_timeout")
            self.assertEqual(raised.exception.stage, "stream")
            self.assertEqual(raised.exception.elapsed, 160.0)
            self.assertTrue((raised.exception.request_id or "").startswith("pdi-idle-"))

    def test_steady_drip_cannot_extend_sse_or_json_past_overall_deadline(self) -> None:
        class DripResponse:
            status = 200

            def __init__(self, body: bytes, content_type: str) -> None:
                self.body = body
                self.offset = 0
                self.headers = Message()
                self.headers["Content-Type"] = content_type
                self.closed = threading.Event()

            def getcode(self):
                return self.status

            def read1(self, _limit):
                if self.closed.is_set() or self.offset >= len(self.body):
                    return b""
                time.sleep(0.01)
                chunk = self.body[self.offset:self.offset + 1]
                self.offset += 1
                return chunk

            def close(self):
                self.closed.set()

        scenarios = (
            (b"event: image_generation.completed", "text/event-stream"),
            (b'{"data":[{"b64_json":"slow"}]}', "application/json"),
        )
        for body, content_type in scenarios:
            with self.subTest(content_type=content_type):
                response = DripResponse(body, content_type)
                opener = Mock()
                opener.open.return_value = response
                wall_started = time.monotonic()
                with patch.object(
                    generate_image,
                    "build_opener",
                    return_value=opener,
                ), self.assertRaises(generate_image.RequestError) as raised:
                    generate_image._post(
                        KEY,
                        b"{}",
                        0.03,
                        idle_timeout=0.2,
                        heartbeat_interval=0,
                        client_request_id="pdi-steady-drip",
                    )
                self.assertEqual(raised.exception.kind, "overall_timeout")
                self.assertLess(time.monotonic() - wall_started, 0.2)
                opener.open.assert_called_once()

    def test_blocking_fallback_read_cannot_extend_json_past_overall_deadline(self) -> None:
        class BlockingResponse:
            status = 200

            def __init__(self) -> None:
                self.headers = Message()
                self.headers["Content-Type"] = "application/json"
                self.release = threading.Event()

            def getcode(self):
                return self.status

            def read(self, _limit):
                self.release.wait(0.5)
                return b'{"data":[]}'

            def close(self):
                pass

        response = BlockingResponse()
        opener = Mock()
        opener.open.return_value = response
        wall_started = time.monotonic()
        try:
            with patch.object(
                generate_image,
                "build_opener",
                return_value=opener,
            ), self.assertRaises(generate_image.RequestError) as raised:
                generate_image._post(
                    KEY,
                    b"{}",
                    0.03,
                    idle_timeout=0.2,
                    heartbeat_interval=0,
                )
            self.assertEqual(raised.exception.kind, "overall_timeout")
            self.assertLess(time.monotonic() - wall_started, 0.2)
            opener.open.assert_called_once()
        finally:
            response.release.set()

    def test_blocking_open_respects_connect_and_overall_and_closes_late_response(self) -> None:
        for overall, connect, expected in (
            (0.03, 0.2, "overall_timeout"),
            (0.2, 0.03, "timeout"),
        ):
            with self.subTest(expected=expected):
                release_open = threading.Event()
                late_closed = threading.Event()
                heartbeat_output = io.StringIO()

                class LateResponse:
                    def close(self):
                        late_closed.set()

                opener = Mock()

                def blocking_open(*_args, **_kwargs):
                    release_open.wait(0.5)
                    return LateResponse()

                opener.open.side_effect = blocking_open
                wall_started = time.monotonic()
                with patch.object(
                    generate_image,
                    "build_opener",
                    return_value=opener,
                ), contextlib.redirect_stderr(heartbeat_output), self.assertRaises(
                    generate_image.RequestError
                ) as raised:
                    generate_image._post(
                        KEY,
                        b"{}",
                        overall,
                        connect_timeout=connect,
                        heartbeat_interval=0.01,
                        client_request_id="pdi-blocking-open",
                    )
                returned_at = time.monotonic()
                output_at_return = heartbeat_output.getvalue()
                self.assertEqual(raised.exception.kind, expected)
                self.assertLess(returned_at - wall_started, 0.2)
                opener.open.assert_called_once()
                time.sleep(0.03)
                self.assertEqual(heartbeat_output.getvalue(), output_at_return)
                release_open.set()
                self.assertTrue(late_closed.wait(0.2))

    def test_timeout_return_does_not_wait_for_blocking_response_close(self) -> None:
        class BlockingCloseResponse:
            status = 200

            def __init__(self) -> None:
                self.headers = Message()
                self.headers["Content-Type"] = "application/json"
                self.close_release = threading.Event()

            def getcode(self):
                return self.status

            def read1(self, _limit):
                time.sleep(0.1)
                return b"x"

            def close(self):
                self.close_release.wait(0.5)

        response = BlockingCloseResponse()
        opener = Mock()
        opener.open.return_value = response
        wall_started = time.monotonic()
        try:
            with patch.object(
                generate_image,
                "build_opener",
                return_value=opener,
            ), self.assertRaises(generate_image.RequestError) as raised:
                generate_image._post(
                    KEY,
                    b"{}",
                    0.03,
                    idle_timeout=0.2,
                    heartbeat_interval=0,
                )
            self.assertEqual(raised.exception.kind, "overall_timeout")
            self.assertLess(time.monotonic() - wall_started, 0.2)
            opener.open.assert_called_once()
        finally:
            response.close_release.set()

    def test_local_heartbeat_reports_progress_without_network_retry(self) -> None:
        heartbeat = generate_image._Heartbeat(10.0, "pdi-heartbeat", 20.0)
        heartbeat._stop = Mock()
        heartbeat._stop.wait.side_effect = [False, True]
        stderr = io.StringIO()
        with patch.object(generate_image.time, "monotonic", return_value=30.0), contextlib.redirect_stderr(stderr):
            heartbeat._run()
        self.assertIn("已等待 20 秒", stderr.getvalue())
        self.assertIn("pdi-heartbeat", stderr.getvalue())
        self.assertEqual(heartbeat._stop.wait.call_count, 2)

    def test_image_decode_accepts_a_valid_png_larger_than_four_megapixels(self) -> None:
        image = large_pixel_png()
        raw = json.dumps({
            "type": "image_generation.completed",
            "b64_json": base64.b64encode(image).decode("ascii"),
        }).encode()
        self.assertEqual(generate_image._image_bytes(raw), image)

    def test_post_classifies_transport_failures_without_retry(self) -> None:
        scenarios = (
            (
                "dns",
                generate_image.URLError(
                    socket.gaierror(socket.EAI_NONAME, "name resolution failed")
                ),
            ),
            ("tls", generate_image.URLError(ssl.SSLError("handshake failed"))),
            (
                "connect",
                generate_image.URLError(
                    ConnectionRefusedError(errno.ECONNREFUSED, "connection refused")
                ),
            ),
            ("timeout", socket.timeout("connect timed out")),
            ("connect", http.client.BadStatusLine("bad HTTP status")),
        )
        for expected_kind, failure in scenarios:
            with self.subTest(kind=expected_kind):
                opener = Mock()
                opener.open.side_effect = failure
                with patch.object(
                    generate_image, "build_opener", return_value=opener
                ), self.assertRaises(generate_image.RequestError) as raised:
                    generate_image._post(KEY, b"{}", 900)
                self.assertEqual(raised.exception.status, 0)
                self.assertEqual(raised.exception.kind, expected_kind)
                self.assertFalse(raised.exception.response_started)
                self.assertRegex(raised.exception.request_id or "", r"^pdi-[0-9a-f]{32}$")
                self.assertNotIn(KEY, repr(raised.exception))
                opener.open.assert_called_once()

    def test_post_classifies_failures_after_response_headers_without_retry(self) -> None:
        class Response:
            status = 200

            def __init__(self, failure: BaseException) -> None:
                self.failure = failure
                self.headers = Message()
                self.headers["X-Request-ID"] = "req-safe-123"

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def getcode(self):
                return self.status

            def read(self, _limit):
                raise self.failure

        scenarios = (
            ("idle_timeout", socket.timeout("body timed out")),
            ("connect", ConnectionResetError(errno.ECONNRESET, "connection reset")),
            ("connect", ssl.SSLError("response TLS failure")),
            ("connect", http.client.IncompleteRead(b"partial", 10)),
        )
        for expected_kind, failure in scenarios:
            with self.subTest(kind=expected_kind):
                opener = Mock()
                opener.open.return_value = Response(failure)
                with patch.object(
                    generate_image, "build_opener", return_value=opener
                ), self.assertRaises(generate_image.RequestError) as raised:
                    generate_image._post(KEY, b"{}", 900)
                self.assertEqual(raised.exception.status, 200)
                self.assertEqual(raised.exception.kind, expected_kind)
                self.assertTrue(raised.exception.response_started)
                self.assertEqual(raised.exception.request_id, "req-safe-123")
                opener.open.assert_called_once()

    def test_post_classifies_http_auth_and_server_errors_with_safe_request_id(self) -> None:
        for status in (401, 403, 503):
            with self.subTest(status=status):
                headers = Message()
                headers["X-Request-ID"] = "req-safe-{}".format(status)
                headers["Authorization"] = "Bearer " + KEY
                failure = generate_image.HTTPError(
                    generate_image.ENDPOINT,
                    status,
                    "request failed",
                    headers,
                    io.BytesIO(b"ignored error body"),
                )
                opener = Mock()
                opener.open.side_effect = failure
                with patch.object(
                    generate_image, "build_opener", return_value=opener
                ), self.assertRaises(generate_image.RequestError) as raised:
                    generate_image._post(KEY, b"{}", 1)
                self.assertEqual(raised.exception.status, status)
                self.assertEqual(raised.exception.kind, "http")
                self.assertTrue(raised.exception.response_started)
                self.assertEqual(
                    raised.exception.request_id, "req-safe-{}".format(status)
                )
                self.assertNotIn(KEY, repr(raised.exception))
                opener.open.assert_called_once()

    def test_post_classifies_json_memory_guard_as_local_resource_failure(self) -> None:
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def getcode(self):
                return self.status

            def read(self, _limit):
                return b"too-large"

        opener = Mock()
        opener.open.return_value = Response()
        with patch.object(generate_image, "build_opener", return_value=opener), self.assertRaises(
            generate_image.LocalResourceError
        ) as raised:
            generate_image._post(KEY, b"{}", 1, json_memory_limit=1)
        self.assertEqual(raised.exception.code, "json_memory_limit")
        self.assertEqual(raised.exception.stage, "stream")
        opener.open.assert_called_once()

    def test_post_opens_the_fixed_images_endpoint_exactly_once(self) -> None:
        class Response:
            status = 200

            def __init__(self):
                self.served = False

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def getcode(self):
                return self.status

            def read(self, _limit):
                if self.served:
                    return b""
                self.served = True
                return b"{}"

        opener = Mock()
        opener.open.return_value = Response()
        body = b'{"prompt":"a dog"}'
        with patch.object(generate_image, "build_opener", return_value=opener) as build:
            result = generate_image._post(
                KEY,
                body,
                12.5,
                client_request_id="pdi-test-request",
            )
            self.assertEqual(result.body, b"{}")
            self.assertEqual(result.request_id, "pdi-test-request")

        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://portdan.com/v1/images/generations")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.data, body)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 12.5)
        self.assertEqual(request.get_header("Authorization"), "Bearer " + KEY)
        self.assertEqual(request.get_header("X-request-id"), "pdi-test-request")
        self.assertEqual(request.get_header("Accept"), "text/event-stream")
        self.assertEqual(
            request.get_header("User-agent"),
            "portdan-image2-runner/5.0",
        )
        self.assertTrue(
            any(isinstance(item, generate_image.ProxyHandler) for item in build.call_args.args)
        )
        self.assertTrue(
            any(isinstance(item, generate_image._NoRedirect) for item in build.call_args.args)
        )

    def test_post_uses_fifteen_second_connection_timeout_by_default(self) -> None:
        opener = Mock()
        opener.open.return_value = HTTPBodyResponse(b"{}")
        with patch.object(generate_image, "build_opener", return_value=opener):
            generate_image._post(KEY, b"{}", 900, heartbeat_interval=0)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 15.0)

    def test_response_socket_switches_to_sixty_second_idle_timeout(self) -> None:
        class Node:
            pass

        sock = Mock()
        response, first, second, raw = Node(), Node(), Node(), Node()
        response.fp = first
        first.fp = second
        second.raw = raw
        raw._sock = sock
        generate_image._set_response_socket_timeout(response, 60.0)
        sock.settimeout.assert_called_once_with(60.0)

    def test_http_errors_do_not_retry(self) -> None:
        request_config = generate_image.RequestConfig(api_key=KEY, model=MODEL)
        with patch.object(
            generate_image, "resolve_request_config", return_value=request_config
        ), patch.object(
            generate_image, "_post", side_effect=generate_image.RequestError(429)
        ) as post, patch.object(
            sys, "stdin", io.StringIO("a dog")
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = generate_image.main(["--prompt-stdin", "--quality", "medium"])
        self.assertEqual(code, 4)
        self.assertEqual(post.call_count, 1)

    def test_auth_and_server_failures_submit_only_once(self) -> None:
        request_config = generate_image.RequestConfig(api_key=KEY, model=MODEL)
        for status in (0, 401, 403, 500):
            with self.subTest(status=status), patch.object(
                generate_image, "resolve_request_config", return_value=request_config
            ), patch.object(
                generate_image, "_post", side_effect=generate_image.RequestError(status)
            ) as post, patch.object(sys, "stdin", io.StringIO("a dog")), contextlib.redirect_stdout(
                io.StringIO()
            ), contextlib.redirect_stderr(io.StringIO()):
                code = generate_image.main(["--prompt-stdin", "--quality", "high"])
            self.assertEqual(post.call_count, 1)
            self.assertIn(code, (3, 4))

    def test_user_key_auth_transport_and_server_failures_submit_only_once(self) -> None:
        scenarios = (
            (generate_image.RequestError(401, kind="http", response_started=True), 3),
            (generate_image.RequestError(403, kind="http", response_started=True), 3),
            (generate_image.RequestError(0, kind="dns"), 4),
            (generate_image.RequestError(0, kind="tls"), 4),
            (generate_image.RequestError(0, kind="connect"), 4),
            (generate_image.RequestError(0, kind="timeout"), 4),
            (
                generate_image.RequestError(
                    0, kind="response_timeout", response_started=True
                ),
                4,
            ),
            (generate_image.RequestError(503, kind="http", response_started=True), 4),
        )
        for failure, expected_code in scenarios:
            with self.subTest(kind=failure.kind, status=failure.status):
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.dict(os.environ, {}, clear=True), patch.object(
                    generate_image, "_post", side_effect=failure
                ) as post, patch.object(
                    sys, "stdin", io.StringIO("a dog\n{}\n".format(KEY))
                ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = generate_image.main([
                        "--api-key-stdin", "--quality", "high"
                    ])
                    key_remains = "PORTDAN_API_KEY" in os.environ
                self.assertEqual(code, expected_code)
                post.assert_called_once()
                self.assertFalse(key_remains)
                self.assertEqual(stdout.getvalue(), "")
                self.assertNotIn(KEY, stderr.getvalue())

    def test_transport_and_server_messages_are_classified_without_legacy_guess(self) -> None:
        failures = {
            "dns": generate_image.RequestError(0, kind="dns"),
            "tls": generate_image.RequestError(0, kind="tls"),
            "connect": generate_image.RequestError(0, kind="connect"),
            "timeout": generate_image.RequestError(0, kind="timeout"),
            "response_timeout": generate_image.RequestError(
                0, kind="response_timeout", response_started=True
            ),
            "server": generate_image.RequestError(
                503, kind="http", response_started=True, request_id="req-safe-503"
            ),
        }
        messages = {
            name: generate_image._error_message(failure)
            for name, failure in failures.items()
        }
        self.assertIn("域名解析", messages["dns"])
        self.assertIn("TLS", messages["tls"])
        self.assertIn("连接", messages["connect"])
        self.assertIn("超时", messages["timeout"])
        self.assertIn("超时", messages["response_timeout"])
        self.assertIn("503", messages["server"])
        self.assertIn("req-safe-503", messages["server"])
        for message in messages.values():
            self.assertNotIn("可能已经到达后台", message)

    def test_key_never_appears_in_cli_failure_output(self) -> None:
        request_config = generate_image.RequestConfig(api_key=KEY, model=MODEL)
        scenarios = (
            generate_image.RequestError(401),
            generate_image.RequestError(404),
            generate_image.RequestError(429),
            generate_image.RequestError(500),
        )
        for failure in scenarios:
            with self.subTest(status=failure.status):
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.object(
                    generate_image, "resolve_request_config", return_value=request_config
                ), patch.object(
                    generate_image, "_post", side_effect=failure
                ), patch.object(
                    sys, "stdin", io.StringIO("a dog")
                ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    generate_image.main(["--prompt-stdin", "--quality", "low"])
                self.assertNotIn(KEY, stdout.getvalue())
                self.assertNotIn(KEY, stderr.getvalue())

    def test_404_message_is_factual_and_does_not_guess_channel_state(self) -> None:
        message = generate_image._error_message(generate_image.RequestError(404))
        self.assertTrue(message.startswith("Portdan 返回 404，图片请求未完成"))
        self.assertNotIn("图片通道", message)

    def test_main_does_not_modify_codex_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            codex = home / ".codex"
            self.write_config(
                codex,
                'openai_base_url = "https://portdan.com"\n',
                {"OPENAI_API_KEY": KEY},
            )
            before = hashlib.sha256((codex / "config.toml").read_bytes()).hexdigest()
            with patch.object(Path, "home", return_value=home), patch.dict(
                os.environ, {}, clear=True
            ), patch.object(generate_image, "_post", return_value=response_body()), patch.object(
                Path, "cwd", return_value=home
            ), patch.object(sys, "stdin", io.StringIO("a dog")), contextlib.redirect_stdout(
                io.StringIO()
            ), contextlib.redirect_stderr(io.StringIO()):
                code = generate_image.main(["--prompt-stdin", "--quality", "low"])
            self.assertEqual(code, 0)
            self.assertEqual(hashlib.sha256((codex / "config.toml").read_bytes()).hexdigest(), before)

    def test_raw_request_preserves_fields_and_only_normalizes_transport(self) -> None:
        request = {
            "model": "gpt-image-17.2-preview",
            "prompt": {"parts": ["a", 3, None, True]},
            "n": 37,
            "size": "future-size",
            "quality": {"tier": 9},
            "background": None,
            "moderation": False,
            "vendor_extension": {"nested": [1.25, {"enabled": True}]},
            "stream": False,
            "response_format": "url",
        }
        original = json.loads(json.dumps(request))
        body, requested = generate_image._raw_payload(request)
        sent = json.loads(body)
        expected = dict(original)
        expected["stream"] = True
        expected["response_format"] = "b64_json"
        self.assertEqual(sent, expected)
        self.assertEqual(request, original)
        self.assertEqual(requested, 37)

    def test_portdan_parameter_examples_are_forwarded_without_local_enums(self) -> None:
        for count, size in ((1, "auto"), (6, "4K"), (100, "4096x2304")):
            with self.subTest(count=count, size=size):
                request = {
                    "prompt": "x",
                    "n": count,
                    "size": size,
                    "quality": "auto",
                    "background": "transparent",
                    "output_format": "webp",
                    "output_compression": 73,
                    "partial_images": 3,
                    "future_option": {"enabled": True, "value": None},
                }
                body, requested = generate_image._raw_payload(request)
                sent = json.loads(body)
                self.assertEqual(requested, count)
                for key, value in request.items():
                    self.assertEqual(sent[key], value)
                self.assertIs(sent["stream"], True)
                self.assertEqual(sent["response_format"], "b64_json")

    def test_raw_n_is_forwarded_but_requested_only_tracks_positive_integer(self) -> None:
        for value, expected in ((0, None), (-2, None), (True, None), ("4", None), (4, 4)):
            with self.subTest(value=value):
                body, requested = generate_image._raw_payload({"prompt": "x", "n": value})
                self.assertEqual(json.loads(body)["n"], value)
                self.assertEqual(requested, expected)

    def test_request_model_is_gpt_images_family_only(self) -> None:
        for model in ("gpt-image-1", "gpt-image-1.5", "gpt-image-2", "gpt-image-99-preview"):
            with self.subTest(model=model):
                body, _ = generate_image._raw_payload({"model": model, "prompt": "x"})
                self.assertEqual(json.loads(body)["model"], model)
        for model in ("grok-2-image", "gpt-4o", None, 7, True):
            with self.subTest(model=model), self.assertRaises(generate_image.InputError):
                generate_image._raw_payload({"model": model, "prompt": "x"})

    def test_request_json_rejects_duplicate_fields_and_nonstandard_numbers(self) -> None:
        for raw in (
            b'{"prompt":"a","prompt":"b"}',
            b'{"nested":{"x":1,"x":2}}',
            b'{"prompt":"x","value":NaN}',
            b'{"prompt":"x","value":Infinity}',
        ):
            with self.subTest(raw=raw), self.assertRaises(generate_image.InputError):
                generate_image._parse_request_json(raw)

    def test_request_json_stdin_framing_keeps_key_outside_json(self) -> None:
        compact = json.dumps({"prompt": "x", "api_key": "not-auth"}, separators=(",", ":"))
        with patch.object(sys, "stdin", io.StringIO(compact + "\n" + KEY + "\n")):
            request, key = generate_image._request_json_and_key_from_stdin()
        self.assertEqual(request["api_key"], "not-auth")
        self.assertEqual(key, KEY)

        with patch.object(sys, "stdin", io.StringIO(compact + "\n")):
            with self.assertRaises(generate_image.ProvidedKeyError):
                generate_image._request_json_and_key_from_stdin()
        with patch.object(sys, "stdin", io.StringIO(compact + "\n" + KEY + "\nextra")):
            with self.assertRaises(generate_image.InputError) as raised:
                generate_image._request_json_and_key_from_stdin()
        self.assertEqual(raised.exception.code, "trailing_stdin_data")
        with patch.object(sys, "stdin", io.StringIO('{\n"prompt":"x"}\n' + KEY + "\n")):
            with self.assertRaises(generate_image.InputError):
                generate_image._request_json_and_key_from_stdin()

    def test_request_json_stdin_alone_accepts_multiline_object_to_eof(self) -> None:
        raw = '{\n  "prompt": "x",\n  "nested": {"a": true}\n}'
        with patch.object(sys, "stdin", io.StringIO(raw)):
            value = generate_image._request_json_from_stdin()
        self.assertEqual(value, {"prompt": "x", "nested": {"a": True}})

    def test_request_json_and_one_time_key_use_exact_two_record_main_framing(self) -> None:
        raw_request = json.dumps(
            {"model": "gpt-image-2.8", "prompt": "x", "custom": {"flag": True}},
            separators=(",", ":"),
        )

        def post_once(api_key, body, _timeout, **kwargs):
            self.assertEqual(api_key, KEY)
            self.assertEqual(json.loads(body)["custom"], {"flag": True})
            self.assertNotIn(KEY, body.decode("utf-8"))
            kwargs["on_image"](
                generate_image.EncodedImage(base64.b64encode(PNG).decode("ascii"), {})
            )
            return generate_image.PostResult(body=b"", request_id="pdi-two-records")

        with tempfile.TemporaryDirectory() as temp:
            stdout = io.StringIO()
            with patch.object(generate_image, "resolve_request_config") as resolve, patch.object(
                generate_image, "_post", side_effect=post_once
            ) as post, patch.object(Path, "cwd", return_value=Path(temp)), patch.object(
                sys, "stdin", io.StringIO(raw_request + "\n" + KEY + "\n")
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                code = generate_image.main([
                    "--request-json-stdin", "--api-key-stdin", "--json"
                ])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout.getvalue())["status"], "completed")
            resolve.assert_not_called()
            post.assert_called_once()

    def test_json_success_is_one_stable_safe_object(self) -> None:
        jpeg = b"\xff\xd8\xff\xe0fake-jpeg"

        def post_once(api_key, body, timeout, **kwargs):
            self.assertEqual(api_key, KEY)
            self.assertIsNone(timeout)
            self.assertEqual(json.loads(body)["vendor"], {"enabled": True})
            kwargs["on_image"](
                generate_image.EncodedImage(
                    base64.b64encode(jpeg).decode("ascii"),
                    {"output_format": "png"},
                )
            )
            return generate_image.PostResult(
                body=b"", request_id="pdi-json-safe", image_count=1
            )

        request = {"prompt": "secret prompt", "vendor": {"enabled": True}, "n": 1}
        with tempfile.TemporaryDirectory() as temp:
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(
                generate_image,
                "resolve_request_config",
                return_value=generate_image.RequestConfig(api_key=KEY),
            ), patch.object(generate_image, "_post", side_effect=post_once) as post, patch.object(
                Path, "cwd", return_value=Path(temp)
            ), patch.object(
                sys, "stdin", io.StringIO(json.dumps(request))
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = generate_image.main(["--request-json-stdin", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(len(stdout.getvalue().splitlines()), 1)
            result = json.loads(stdout.getvalue())
            self.assertEqual(list(result), [
                "schema", "status", "error", "request_id", "requested", "completed",
                "artifacts", "diagnostics", "elapsed_seconds",
            ])
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["request_id"], "pdi-json-safe")
            self.assertEqual(result["requested"], 1)
            self.assertEqual(result["completed"], 1)
            self.assertEqual(result["artifacts"][0]["format"], "jpeg")
            self.assertTrue(result["artifacts"][0]["path"].endswith(".jpeg"))
            self.assertIsNone(result["diagnostics"])
            self.assertNotIn("secret prompt", stdout.getvalue() + stderr.getvalue())
            self.assertNotIn(KEY, stdout.getvalue() + stderr.getvalue())
            self.assertNotIn(base64.b64encode(jpeg).decode(), stdout.getvalue())
            post.assert_called_once()

    def test_json_error_is_one_safe_object_without_raw_exception(self) -> None:
        raw_marker = "raw-upstream-secret"
        with tempfile.TemporaryDirectory() as temp:
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(
                generate_image,
                "resolve_request_config",
                return_value=generate_image.RequestConfig(api_key=KEY),
            ), patch.object(
                generate_image,
                "_post",
                side_effect=generate_image.RequestError(
                    503,
                    kind="http",
                    request_id="pdi-safe-error",
                    stage="headers",
                ),
            ) as post, patch.object(Path, "cwd", return_value=Path(temp)), patch.object(
                sys, "stdin", io.StringIO("prompt with " + raw_marker)
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = generate_image.main(["--prompt-stdin", "--json"])
            self.assertEqual(code, 4)
            post.assert_called_once()
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["status"], "error")
            self.assertEqual(set(result["error"]), {"code", "stage"})
            self.assertNotIn(raw_marker, stdout.getvalue())
            self.assertNotIn(KEY, stdout.getvalue() + stderr.getvalue())

    def test_json_partial_publishes_completed_artifacts_once(self) -> None:
        def partial_post(_key, _body, _timeout, **kwargs):
            kwargs["on_image"](
                generate_image.EncodedImage(base64.b64encode(PNG).decode("ascii"), {})
            )
            raise generate_image.PartialImageError(
                1, 2, request_id="pdi-partial-json", elapsed=3.0
            )

        with tempfile.TemporaryDirectory() as temp:
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(
                generate_image,
                "resolve_request_config",
                return_value=generate_image.RequestConfig(api_key=KEY),
            ), patch.object(generate_image, "_post", side_effect=partial_post) as post, patch.object(
                Path, "cwd", return_value=Path(temp)
            ), patch.object(sys, "stdin", io.StringIO("x")), contextlib.redirect_stdout(
                stdout
            ), contextlib.redirect_stderr(stderr):
                code = generate_image.main(["--prompt-stdin", "--count", "2", "--json"])
            self.assertEqual(code, 7)
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["requested"], 2)
            self.assertEqual(result["completed"], 1)
            artifact = result["artifacts"][0]
            self.assertEqual(Path(artifact["path"]).read_bytes(), PNG)
            self.assertEqual(artifact["format"], "png")
            self.assertIn("1/2", stderr.getvalue())
            post.assert_called_once()

    def test_artifact_format_uses_bytes_and_unknown_is_bin_even_with_fake_hint(self) -> None:
        payloads = (
            (PNG, "png"),
            (b"\xff\xd8\xffjpeg", "jpeg"),
            (b"RIFF\x04\x00\x00\x00WEBPdata", "webp"),
            (b"not-an-image", "bin"),
        )
        with tempfile.TemporaryDirectory() as temp, patch.object(Path, "cwd", return_value=Path(temp)):
            with generate_image._ImageBatch(len(payloads)) as batch:
                for raw, _expected_format in payloads:
                    batch.stage_payload(
                        generate_image.EncodedImage(
                            base64.b64encode(raw).decode("ascii"),
                            {"output_format": "png", "mime_type": "image/png"},
                        )
                    )
                paths = batch.publish()
                artifacts = batch.artifacts
            self.assertEqual([artifact.format for artifact in artifacts], [item[1] for item in payloads])
            self.assertEqual([path.suffix for path in paths], [".png", ".jpeg", ".webp", ".bin"])
            self.assertEqual([path.read_bytes() for path in paths], [item[0] for item in payloads])
            self.assertEqual(len(list((Path(temp) / "portdan-images").iterdir())), 1)

    def test_empty_decoded_image_is_malformed_and_publishes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(Path, "cwd", return_value=root), self.assertRaises(
                generate_image.ResponseError
            ):
                with generate_image._ImageBatch(1) as batch:
                    batch.stage_payload(generate_image.EncodedImage("", {}))
            output = root / "portdan-images"
            self.assertTrue(output.is_dir())
            self.assertEqual(list(output.iterdir()), [])

    def test_diagnose_is_read_only_and_reports_source_without_key_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(
                generate_image,
                "resolve_request_config",
                return_value=generate_image.RequestConfig(
                    api_key=KEY, source=generate_image.KEY_SOURCE_ENV
                ),
            ) as resolve, patch.object(generate_image, "_post") as post, patch.object(
                Path, "cwd", return_value=root
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = generate_image.main(["--diagnose", "--json"])
            self.assertEqual(code, 0)
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["status"], "diagnose")
            self.assertIsNone(result["error"])
            self.assertIsNone(result["request_id"])
            self.assertIsNone(result["requested"])
            self.assertEqual(result["completed"], 0)
            self.assertEqual(result["artifacts"], [])
            self.assertEqual(result["diagnostics"], {
                "endpoint": generate_image.ENDPOINT,
                "key_source": generate_image.KEY_SOURCE_ENV,
                "output_directory": str((root / "portdan-images").absolute()),
            })
            self.assertNotIn(KEY, stdout.getvalue() + stderr.getvalue())
            self.assertFalse((root / "portdan-images").exists())
            resolve.assert_called_once()
            post.assert_not_called()

    def test_request_body_limit_is_256_mib_and_checked_before_key_or_network(self) -> None:
        self.assertEqual(generate_image.MAX_REQUEST_BODY_BYTES, 256 * 1024 * 1024)
        with patch.object(generate_image, "MAX_REQUEST_BODY_BYTES", 32), self.assertRaises(
            generate_image.LocalResourceError
        ) as raised:
            generate_image._serialize_request({"prompt": "x" * 40})
        self.assertEqual(raised.exception.code, "request_body_too_large")

        stdout = io.StringIO()
        with patch.object(
            generate_image, "MAX_REQUEST_BODY_BYTES", 32
        ), patch.object(generate_image, "resolve_request_config") as resolve, patch.object(
            generate_image, "_post"
        ) as post, patch.object(
            sys, "stdin", io.StringIO('{"prompt":"' + ("x" * 40) + '"}')
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = generate_image.main(["--request-json-stdin", "--json"])
        self.assertEqual(code, 6)
        self.assertEqual(json.loads(stdout.getvalue())["error"]["code"], "request_body_too_large")
        resolve.assert_not_called()
        post.assert_not_called()

    def test_embedded_api_key_field_is_never_used_as_authorization(self) -> None:
        stdout = io.StringIO()
        with patch.object(
            generate_image, "resolve_request_config", side_effect=generate_image.ConfigError()
        ) as resolve, patch.object(generate_image, "_post") as post, patch.object(
            sys,
            "stdin",
            io.StringIO(json.dumps({"prompt": "x", "api_key": KEY})),
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = generate_image.main(["--request-json-stdin", "--json"])
        self.assertEqual(code, 2)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["error"]["code"], "missing_api_key")
        self.assertNotIn(KEY, stdout.getvalue())
        resolve.assert_called_once()
        post.assert_not_called()

    def test_invalid_raw_json_emits_one_json_error_before_network(self) -> None:
        stdout = io.StringIO()
        with patch.object(generate_image, "resolve_request_config") as resolve, patch.object(
            generate_image, "_post"
        ) as post, patch.object(
            sys, "stdin", io.StringIO('{"model":"gpt-image-2","model":"grok"}')
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = generate_image.main(["--request-json-stdin", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(len(stdout.getvalue().splitlines()), 1)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["error"], {"code": "duplicate_request_field", "stage": "input"})
        resolve.assert_not_called()
        post.assert_not_called()

    def test_default_post_has_connect_and_idle_deadlines_but_no_overall_deadline(self) -> None:
        encoded = base64.b64encode(PNG).decode("ascii")
        stream = (
            "event: image_generation.completed\n"
            'data: {"type":"image_generation.completed","b64_json":"'
            + encoded
            + '"}\n\ndata: [DONE]\n\n'
        ).encode("ascii")
        opener = Mock()
        opener.open.return_value = HTTPBodyResponse(stream, content_type="text/event-stream")
        socket_timeouts = []
        with patch.object(generate_image, "build_opener", return_value=opener), patch.object(
            generate_image,
            "_set_response_socket_timeout",
            side_effect=lambda _response, value: socket_timeouts.append(value),
        ):
            result = generate_image._post(KEY, b"{}", heartbeat_interval=0, expected_count=1)
        self.assertEqual(generate_image._image_bytes(result.body), PNG)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 15.0)
        self.assertIn(1800.0, socket_timeouts)
        opener.open.assert_called_once()

    def test_omitted_n_with_completed_image_and_normal_eof_is_success(self) -> None:
        encoded = base64.b64encode(PNG).decode("ascii")
        stream = (
            "event: image_generation.completed\n"
            'data: {"type":"image_generation.completed","b64_json":"'
            + encoded
            + '"}\n\n'
        ).encode("ascii")
        opener = Mock()
        opener.open.return_value = HTTPBodyResponse(
            stream,
            content_type="text/event-stream",
            request_id="pdi-unknown-count-eof",
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(
                generate_image,
                "resolve_request_config",
                return_value=generate_image.RequestConfig(api_key=KEY),
            ), patch.object(
                generate_image, "build_opener", return_value=opener
            ), patch.object(
                Path, "cwd", return_value=root
            ), patch.object(
                sys, "stdin", io.StringIO('{"prompt":"x"}')
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = generate_image.main(["--request-json-stdin", "--json"])
            result = json.loads(stdout.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(result["status"], "completed")
            self.assertIsNone(result["requested"])
            self.assertEqual(result["completed"], 1)
            self.assertEqual(len(result["artifacts"]), 1)
            self.assertEqual(Path(result["artifacts"][0]["path"]).read_bytes(), PNG)
            self.assertNotIn("流中断", stderr.getvalue())
        opener.open.assert_called_once()

    def test_provider_json_is_strict_in_sse_and_json_main_paths(self) -> None:
        encoded = base64.b64encode(PNG).decode("ascii")
        cases = (
            (
                "sse_duplicate_type",
                "text/event-stream",
                (
                    'event: image_generation.completed\n'
                    'data: {"type":"error","type":"image_generation.completed",'
                    '"b64_json":"' + encoded + '"}\n\n'
                ).encode("ascii"),
            ),
            (
                "sse_duplicate_b64",
                "text/event-stream",
                (
                    'event: image_generation.completed\n'
                    'data: {"type":"image_generation.completed","b64_json":"'
                    + encoded + '","b64_json":"' + encoded + '"}\n\n'
                ).encode("ascii"),
            ),
            (
                "sse_nan",
                "text/event-stream",
                (
                    'event: image_generation.completed\n'
                    'data: {"type":"image_generation.completed","b64_json":"'
                    + encoded + '","score":NaN}\n\n'
                ).encode("ascii"),
            ),
            (
                "json_duplicate_data",
                "application/json",
                (
                    '{"data":[],"data":[{"b64_json":"' + encoded + '"}]}'
                ).encode("ascii"),
            ),
            (
                "json_duplicate_b64",
                "application/json",
                (
                    '{"data":[{"b64_json":"' + encoded
                    + '","b64_json":"' + encoded + '"}]}'
                ).encode("ascii"),
            ),
            (
                "json_nan",
                "application/json",
                (
                    '{"data":[{"b64_json":"' + encoded + '","score":NaN}]}'
                ).encode("ascii"),
            ),
            (
                "json_infinity",
                "application/json",
                (
                    '{"data":[{"b64_json":"' + encoded + '","score":Infinity}]}'
                ).encode("ascii"),
            ),
        )
        for name, content_type, response_body_bytes in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                opener = Mock()
                opener.open.return_value = HTTPBodyResponse(
                    response_body_bytes,
                    content_type=content_type,
                    request_id="pdi-strict-provider-json",
                )
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.object(
                    generate_image,
                    "resolve_request_config",
                    return_value=generate_image.RequestConfig(api_key=KEY),
                ), patch.object(
                    generate_image, "build_opener", return_value=opener
                ), patch.object(
                    Path, "cwd", return_value=root
                ), patch.object(
                    sys, "stdin", io.StringIO('{"prompt":"x"}')
                ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = generate_image.main(["--request-json-stdin", "--json"])
                result = json.loads(stdout.getvalue())
                self.assertEqual(code, 5)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["error"]["code"], "invalid_response")
                self.assertEqual(result["request_id"], "pdi-strict-provider-json")
                self.assertEqual(result["completed"], 0)
                self.assertEqual(result["artifacts"], [])
                self.assertNotIn(encoded, stdout.getvalue() + stderr.getvalue())
                self.assertEqual(list((root / "portdan-images").iterdir()), [])
                opener.open.assert_called_once()


if __name__ == "__main__":
    unittest.main()
