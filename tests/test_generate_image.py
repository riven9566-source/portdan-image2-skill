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
            ) -> bytes:
                request_configs.append(request_config)
                return original_payload(request_config, prompt, size, quality)

            def post(api_key: str, _body: bytes, _timeout: float) -> bytes:
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
            resolve.assert_called_once()
            post_mock.assert_called_once()
            self.assertEqual(observed, [(KEY, KEY)])
            self.assertEqual(restored_value, OTHER_KEY)
            self.assertEqual(len(request_configs), 1)
            self.assertEqual(request_configs[0].api_key, KEY)
            self.assertEqual(request_configs[0].model, MODEL)
            self.assertEqual(request_configs[0].source, generate_image.KEY_SOURCE_STDIN)
            self.assertNotIn(KEY, repr(request_configs[0]))
            combined_output = stdout.getvalue() + stderr.getvalue()
            self.assertNotIn(KEY, combined_output)
            self.assertNotIn(OTHER_KEY, combined_output)
            self.assertIn("本次提供", stderr.getvalue())
            output = Path(stdout.getvalue().strip())
            self.assertEqual(output.read_bytes(), PNG)

    def test_api_key_stdin_restores_missing_environment_after_failure(self) -> None:
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

    def test_payload_uses_responses_image_tool_and_all_quality_levels(self) -> None:
        self.assertEqual(generate_image.ENDPOINT, "https://portdan.com/v1/responses")
        request_config = generate_image.RequestConfig(api_key=KEY, model=MODEL)
        for quality in generate_image.QUALITIES:
            with self.subTest(quality=quality):
                payload = json.loads(
                    generate_image._payload(
                        request_config, "a dog", "1024x1024", quality
                    )
                )
                self.assertEqual(payload, {
                    "model": MODEL,
                    "input": "a dog",
                    "tools": [{
                        "type": "image_generation",
                        "action": "generate",
                        "model": "gpt-image-2",
                        "quality": quality,
                        "size": "1024x1024",
                        "output_format": "png",
                    }],
                    "tool_choice": {"type": "image_generation"},
                    "store": False,
                    "stream": False,
                })

    def test_main_posts_once_writes_png_and_reports_model_and_elapsed_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            stdout, stderr = io.StringIO(), io.StringIO()
            request = Mock(return_value=response_body())
            request_config = generate_image.RequestConfig(api_key=KEY, model=MODEL)
            with patch.object(
                generate_image, "resolve_request_config", return_value=request_config
            ), patch.object(generate_image, "_post", request), patch.object(
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
            self.assertEqual(payload["model"], MODEL)
            self.assertEqual(payload["tools"][0]["model"], "gpt-image-2")
            self.assertEqual(payload["tools"][0]["quality"], "low")
            output = Path(stdout.getvalue().strip())
            self.assertTrue(output.is_absolute())
            self.assertEqual(output.read_bytes(), PNG)
            self.assertIn("OpenAI gpt-image-2（快速）", stderr.getvalue())
            self.assertIn("耗时 2.2 秒", stderr.getvalue())

    def test_existing_output_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            with patch.object(Path, "cwd", return_value=directory):
                first = generate_image._save_png(PNG)
                second = generate_image._save_png(PNG)
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_bytes(), PNG)

    def test_output_link_failure_keeps_a_complete_recovery_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.object(Path, "cwd", return_value=Path(temp)):
            with patch.object(generate_image.os, "link", side_effect=OSError("unsupported")):
                with self.assertRaises(generate_image.OutputRecoveryError) as raised:
                    generate_image._save_png(PNG)
            recovery = raised.exception.path
            self.assertTrue(recovery.is_file())
            self.assertEqual(recovery.read_bytes(), PNG)

    def test_png_text_metadata_is_removed(self) -> None:
        iend = PNG.rfind(b"IEND") - 4
        decorated = PNG[:iend] + png_chunk(b"tEXt", b"prompt=private") + PNG[iend:]
        cleaned = generate_image._sanitize_png(decorated)
        self.assertNotIn(b"tEXt", cleaned)
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
                    generate_image._post(KEY, b"{}", 1)
                self.assertEqual(raised.exception.status, 0)
                self.assertEqual(raised.exception.kind, expected_kind)
                self.assertFalse(raised.exception.response_started)
                self.assertIsNone(raised.exception.request_id)
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
            ("response_timeout", socket.timeout("body timed out")),
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
                    generate_image._post(KEY, b"{}", 1)
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

    def test_post_classifies_oversized_success_response_without_claiming_rejection(self) -> None:
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
        with patch.object(generate_image, "build_opener", return_value=opener), patch.object(
            generate_image, "MAX_RESPONSE_BYTES", 1
        ), self.assertRaises(generate_image.RequestError) as raised:
            generate_image._post(KEY, b"{}", 1)
        self.assertEqual(raised.exception.status, 200)
        self.assertEqual(raised.exception.kind, "response_too_large")
        message = generate_image._error_message(raised.exception)
        self.assertIn("超过安全大小限制", message)
        self.assertNotIn("拒绝", message)
        self.assertNotIn("HTTP 200", message)
        opener.open.assert_called_once()

    def test_post_opens_the_fixed_responses_endpoint_exactly_once(self) -> None:
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def getcode(self):
                return self.status

            def read(self, _limit):
                return b"{}"

        opener = Mock()
        opener.open.return_value = Response()
        body = b'{"prompt":"a dog"}'
        with patch.object(generate_image, "build_opener", return_value=opener) as build:
            self.assertEqual(generate_image._post(KEY, body, 12.5), b"{}")

        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://portdan.com/v1/responses")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.data, body)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 12.5)
        self.assertEqual(request.get_header("Authorization"), "Bearer " + KEY)
        self.assertTrue(
            any(isinstance(item, generate_image.ProxyHandler) for item in build.call_args.args)
        )
        self.assertTrue(
            any(isinstance(item, generate_image._NoRedirect) for item in build.call_args.args)
        )

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


if __name__ == "__main__":
    unittest.main()
