from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
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


def response_body() -> bytes:
    return json.dumps({
        "created": 1,
        "data": [{
            "b64_json": base64.b64encode(PNG).decode("ascii"),
            "revised_prompt": "a dog",
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
    ) -> None:
        directory = home / ".cc-switch"
        directory.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(directory / "cc-switch.db")
        try:
            connection.execute(
                "CREATE TABLE providers "
                "(app_type TEXT, is_current INTEGER, name TEXT, settings_config TEXT)"
            )
            connection.execute(
                "INSERT INTO providers VALUES (?, ?, ?, ?)",
                ("codex", current, name, json.dumps({"config": config, "auth": auth})),
            )
            connection.commit()
        finally:
            connection.close()

    def resolve(self, home: Path, env: dict[str, str] | None = None) -> str:
        with patch.object(Path, "home", return_value=home), patch.dict(
            os.environ, env or {}, clear=True
        ):
            return generate_image.resolve_api_key()

    def test_cc_switch_current_codex_provider_is_first_key_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_cc_database(
                home,
                'openai_base_url = "https://portdan.com"\n',
                {"auth_mode": "apikey", "OPENAI_API_KEY": KEY},
            )
            self.write_config(
                home / ".codex",
                '[model_providers.portdan]\nbase_url = "https://portdan.com"\n'
                'experimental_bearer_token = "{}"\n'.format(OTHER_KEY),
            )
            self.assertEqual(self.resolve(home, {"PORTDAN_API_KEY": OTHER_KEY}), KEY)

    def test_cc_switch_current_provider_needs_only_its_auth_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            directory = home / ".cc-switch"
            directory.mkdir(parents=True)
            connection = sqlite3.connect(directory / "cc-switch.db")
            try:
                connection.execute(
                    "CREATE TABLE providers "
                    "(app_type TEXT, is_current INTEGER, name TEXT, settings_config TEXT)"
                )
                connection.execute(
                    "INSERT INTO providers VALUES (?, ?, ?, ?)",
                    (
                        "codex",
                        1,
                        "Portdan",
                        json.dumps({"auth": {"OPENAI_API_KEY": KEY}}),
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            self.assertEqual(self.resolve(home), KEY)

    def test_named_portdan_cc_switch_provider_uses_auth_before_parsing_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_cc_database(
                home,
                "this provider config does not need to be parsed",
                {"OPENAI_API_KEY": KEY},
            )
            with patch.object(generate_image, "_parse_config") as parse:
                self.assertEqual(self.resolve(home), KEY)
            parse.assert_not_called()

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

    def test_cc_switch_database_config_inline_token_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_cc_database(
                home,
                '[model_providers.portdan]\nexperimental_bearer_token = "{}"\n'.format(KEY),
                {},
            )
            self.assertEqual(self.resolve(home), KEY)

    def test_cc_switch_database_config_env_key_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_cc_database(
                home,
                '[model_providers.portdan]\nenv_key = "PORTDAN_TEST_KEY"\n',
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
                '[model_providers.portdan]\nexperimental_bearer_token = "{}"\n'.format(KEY),
            )
            self.assertEqual(self.resolve(home), KEY)

    def test_inline_portdan_token_needs_no_model_provider_model_or_wire_api(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_config(
                home / ".codex",
                '[model_providers.portdan]\n'
                'experimental_bearer_token = "{}"\n'.format(KEY),
            )
            self.assertEqual(self.resolve(home), KEY)

    def test_inline_portdan_token_does_not_read_auth_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_config(
                home / ".codex",
                '[model_providers.portdan]\n'
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
                'model_provider = "portdan"\n'
                '[model_providers.portdan]\nrequires_openai_auth = true\n'
                '[model_providers.portdan_backup]\n'
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
                'openai_base_url = "https://portdan.com"\n',
                {"auth_mode": "apikey", "OPENAI_API_KEY": KEY, "tokens": {"access_token": "ignored"}},
            )
            self.assertEqual(self.resolve(home), KEY)

    def test_requires_openai_auth_provider_uses_auth_json_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.write_config(
                home / ".codex",
                'model_provider = "portdan"\n[model_providers.portdan]\n'
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
                '[model_providers.portdan]\nexperimental_bearer_token = "{}"\n'.format(KEY),
            )
            code_home = root / "code-home"
            self.write_config(
                code_home,
                '[model_providers.portdan]\nexperimental_bearer_token = "{}"\n'.format(
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
                '[model_providers.portdan]\nexperimental_bearer_token = "{}"\n'.format(KEY),
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

    def test_missing_key_returns_one_actionable_message_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(Path, "home", return_value=home), patch.dict(
                os.environ, {}, clear=True
            ), patch.object(generate_image, "_post") as post, patch.object(
                sys, "stdin", io.StringIO("a dog")
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = generate_image.main(["--prompt-stdin", "--quality", "low"])
            self.assertEqual(code, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue().strip(), generate_image.MISSING_KEY_MESSAGE)
            post.assert_not_called()

    def test_payload_uses_direct_images_api_and_all_quality_levels(self) -> None:
        self.assertEqual(
            generate_image.ENDPOINT, "https://portdan.com/v1/images/generations"
        )
        for quality in generate_image.QUALITIES:
            with self.subTest(quality=quality):
                payload = json.loads(
                    generate_image._payload("a dog", "1024x1024", quality)
                )
                self.assertEqual(payload, {
                    "model": "gpt-image-2",
                    "prompt": "a dog",
                    "n": 1,
                    "size": "1024x1024",
                    "quality": quality,
                    "response_format": "b64_json",
                    "output_format": "png",
                    "stream": False,
                })
                self.assertNotIn("tools", payload)
                self.assertNotIn("input", payload)

    def test_main_posts_once_writes_png_and_reports_model_and_elapsed_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            stdout, stderr = io.StringIO(), io.StringIO()
            request = Mock(return_value=response_body())
            with patch.object(generate_image, "resolve_api_key", return_value=KEY), patch.object(
                generate_image, "_post", request
            ), patch.object(Path, "cwd", return_value=directory), patch.object(
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
            self.assertEqual(payload["model"], "gpt-image-2")
            self.assertEqual(payload["quality"], "low")
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

    def test_direct_images_data_url_is_supported(self) -> None:
        raw = json.dumps({
            "data": [{"url": "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")}]
        }).encode()
        self.assertEqual(generate_image._image_bytes(raw), PNG)

    def test_invalid_direct_images_response_is_reported(self) -> None:
        for raw in (b"{}", b'{"data":[]}', b'{"output":[]}'):
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

    def test_post_opens_the_fixed_images_endpoint_exactly_once(self) -> None:
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
        self.assertEqual(request.full_url, "https://portdan.com/v1/images/generations")
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
        with patch.object(generate_image, "resolve_api_key", return_value=KEY), patch.object(
            generate_image, "_post", side_effect=generate_image.RequestError(429)
        ) as post, patch.object(sys, "stdin", io.StringIO("a dog")), contextlib.redirect_stdout(
            io.StringIO()
        ), contextlib.redirect_stderr(io.StringIO()):
            code = generate_image.main(["--prompt-stdin", "--quality", "medium"])
        self.assertEqual(code, 4)
        self.assertEqual(post.call_count, 1)

    def test_auth_and_server_failures_submit_only_once(self) -> None:
        for status in (0, 401, 403, 500):
            with self.subTest(status=status), patch.object(
                generate_image, "resolve_api_key", return_value=KEY
            ), patch.object(
                generate_image, "_post", side_effect=generate_image.RequestError(status)
            ) as post, patch.object(sys, "stdin", io.StringIO("a dog")), contextlib.redirect_stdout(
                io.StringIO()
            ), contextlib.redirect_stderr(io.StringIO()):
                code = generate_image.main(["--prompt-stdin", "--quality", "high"])
            self.assertEqual(post.call_count, 1)
            self.assertIn(code, (3, 4))

    def test_key_never_appears_in_cli_failure_output(self) -> None:
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
                    generate_image, "resolve_api_key", return_value=KEY
                ), patch.object(
                    generate_image, "_post", side_effect=failure
                ), patch.object(
                    sys, "stdin", io.StringIO("a dog")
                ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    generate_image.main(["--prompt-stdin", "--quality", "low"])
                self.assertNotIn(KEY, stdout.getvalue())
                self.assertNotIn(KEY, stderr.getvalue())

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
