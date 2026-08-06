from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import tempfile
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
    def config(self, root: Path, key: str = KEY) -> None:
        (root / "config.toml").write_text(
            'model = "gpt-5.6-sol"\n'
            'model_provider = "portdan"\n\n'
            '[model_providers.portdan]\n'
            'base_url = "https://portdan.com"\n'
            'wire_api = "responses"\n'
            'experimental_bearer_token = "{}"\n'.format(key),
            encoding="utf-8",
        )

    def test_resolver_uses_cc_switch_custom_directory_before_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            custom = home / "custom codex"
            custom.mkdir()
            default = home / ".codex"
            default.mkdir()
            (home / ".cc-switch").mkdir()
            (home / ".cc-switch" / "settings.json").write_text(
                json.dumps({"codexConfigDir": str(custom)}), encoding="utf-8"
            )
            self.config(custom)
            self.config(default, "wrong-key-123456")
            with patch.object(Path, "home", return_value=home):
                provider = generate_image.resolve_provider()
            self.assertEqual(provider.api_key, KEY)
            self.assertEqual(provider.model, "gpt-5.6-sol")

    def test_settings_without_custom_directory_uses_default_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / ".cc-switch").mkdir()
            (home / ".cc-switch" / "settings.json").write_text("{}", encoding="utf-8")
            codex = home / ".codex"
            codex.mkdir()
            self.config(codex)
            with patch.object(Path, "home", return_value=home):
                provider = generate_image.resolve_provider()
            self.assertEqual(provider.api_key, KEY)

    def test_code_home_and_portdan_env_do_not_override_active_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            codex = home / ".codex"
            codex.mkdir()
            self.config(codex)
            with patch.object(Path, "home", return_value=home), patch.dict(
                os.environ,
                {"CODEX_HOME": str(home / "wrong"), "PORTDAN_API_KEY": "wrong-key-123456"},
            ):
                provider = generate_image.resolve_provider()
            self.assertEqual(provider.api_key, KEY)

    def test_environment_and_auth_fields_are_not_key_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            codex = home / ".codex"
            codex.mkdir()
            (codex / "config.toml").write_text(
                'model = "gpt-5.6-sol"\nmodel_provider = "portdan"\n'
                '[model_providers.portdan]\nbase_url = "https://portdan.com"\n'
                'wire_api = "responses"\nenv_key = "OTHER_KEY"\nrequires_openai_auth = true\n',
                encoding="utf-8",
            )
            with patch.object(Path, "home", return_value=home), patch.dict(
                os.environ, {"PORTDAN_API_KEY": KEY, "OTHER_KEY": KEY}
            ):
                with self.assertRaises(generate_image.ConfigError):
                    generate_image.resolve_provider()

    def test_auth_json_is_not_read_even_when_it_contains_an_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            codex = home / ".codex"
            codex.mkdir()
            (codex / "config.toml").write_text(
                'model = "gpt-5.6-sol"\nmodel_provider = "portdan"\n'
                '[model_providers.portdan]\nbase_url = "https://portdan.com"\n'
                'wire_api = "responses"\nrequires_openai_auth = true\n',
                encoding="utf-8",
            )
            (codex / "auth.json").write_text(
                json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": KEY}),
                encoding="utf-8",
            )
            with patch.object(Path, "home", return_value=home):
                with self.assertRaises(generate_image.ConfigError):
                    generate_image.resolve_provider()

    def test_python39_fallback_supports_a_quoted_provider_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            codex = home / ".codex"
            codex.mkdir()
            (codex / "config.toml").write_text(
                'model = "gpt-5.6-sol"\nmodel_provider = "Portdan AI 聚合平台"\n'
                '[model_providers."Portdan AI 聚合平台"]\n'
                'base_url = "https://portdan.com/v1"\nwire_api = "responses"\n'
                'experimental_bearer_token = "{}"\n'.format(KEY),
                encoding="utf-8",
            )
            with patch.object(Path, "home", return_value=home), patch.object(
                generate_image, "tomllib", None
            ):
                provider = generate_image.resolve_provider()
            self.assertEqual(provider.api_key, KEY)

    def test_missing_provider_key_returns_fixed_message_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / ".codex").mkdir()
            (home / ".codex" / "config.toml").write_text(
                'model = "gpt-5.6-sol"\nmodel_provider = "portdan"\n'
                '[model_providers.portdan]\nbase_url = "https://portdan.com"\n',
                encoding="utf-8",
            )
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(Path, "home", return_value=home), patch.object(
                generate_image, "_post"
            ) as post, patch.object(sys, "stdin", io.StringIO("a dog")), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = generate_image.main(["--prompt-stdin"])
            self.assertEqual(code, 2)
            self.assertEqual(stderr.getvalue().strip(), generate_image.MISSING_KEY_MESSAGE)
            post.assert_not_called()

    def test_non_portdan_or_missing_wire_api_never_reaches_network(self) -> None:
        cases = (
            'base_url = "https://api.openai.com/v1"\nwire_api = "responses"\n',
            'base_url = "https://portdan.com"\n',
        )
        for fields in cases:
            with self.subTest(fields=fields), tempfile.TemporaryDirectory() as temp:
                home = Path(temp)
                codex = home / ".codex"
                codex.mkdir()
                (codex / "config.toml").write_text(
                    'model = "gpt-5.6-sol"\nmodel_provider = "portdan"\n'
                    '[model_providers.portdan]\n' + fields +
                    'experimental_bearer_token = "{}"\n'.format(KEY),
                    encoding="utf-8",
                )
                stderr = io.StringIO()
                with patch.object(Path, "home", return_value=home), patch.object(
                    generate_image, "_post"
                ) as post, patch.object(sys, "stdin", io.StringIO("a dog")), contextlib.redirect_stderr(stderr):
                    code = generate_image.main(["--prompt-stdin"])
                self.assertEqual(code, 2)
                self.assertEqual(stderr.getvalue().strip(), generate_image.MISSING_KEY_MESSAGE)
                post.assert_not_called()

    def test_payload_and_single_request_are_fixed(self) -> None:
        provider = generate_image.Provider("gpt-5.6-sol", KEY)
        payload = json.loads(generate_image._payload(provider, "a dog", "1024x1024"))
        self.assertEqual(payload["model"], "gpt-5.6-sol")
        self.assertEqual(payload["tools"][0]["model"], "gpt-image-2")
        self.assertEqual(payload["tools"][0]["action"], "generate")
        self.assertEqual(payload["tool_choice"], {"type": "image_generation"})
        self.assertFalse(payload["store"])
        self.assertFalse(payload["stream"])

    def test_main_posts_once_and_writes_new_png_without_touching_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            codex = home / ".codex"
            codex.mkdir()
            self.config(codex)
            config_hash = hashlib.sha256((codex / "config.toml").read_bytes()).hexdigest()
            request = Mock(return_value=response_body())
            with patch.object(Path, "home", return_value=home), patch.object(
                generate_image, "_post", request
            ), patch.object(Path, "cwd", return_value=home), patch.object(
                sys, "stdin", io.StringIO("a dog")
            ), contextlib.redirect_stdout(io.StringIO()):
                code = generate_image.main(["--prompt-stdin"])
            self.assertEqual(code, 0)
            self.assertEqual(request.call_count, 1)
            output = next((home / "portdan-images").glob("*.png"))
            self.assertEqual(output.read_bytes(), PNG)
            self.assertEqual(hashlib.sha256((codex / "config.toml").read_bytes()).hexdigest(), config_hash)

    def test_existing_output_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            directory.mkdir(exist_ok=True)
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

    def test_short_ihdr_is_reported_as_invalid_image_data(self) -> None:
        malformed = (
            b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", b"\x00")
            + png_chunk(b"IEND", b"")
        )
        raw = json.dumps({
            "output": [{
                "type": "image_generation_call",
                "result": base64.b64encode(malformed).decode("ascii"),
            }],
        }).encode()
        stderr = io.StringIO()
        with patch.object(
            generate_image, "resolve_provider", return_value=generate_image.Provider("gpt-5.6-sol", KEY)
        ), patch.object(
            generate_image, "_post", return_value=raw
        ), patch.object(sys, "stdin", io.StringIO("a dog")), contextlib.redirect_stderr(stderr):
            code = generate_image.main(["--prompt-stdin"])
        self.assertEqual(code, 5)
        self.assertEqual(stderr.getvalue().strip(), "Portdan 返回的图片数据无效")

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

    def test_proxy_environment_is_disabled_for_the_authenticated_request(self) -> None:
        provider = generate_image.Provider("gpt-5.6-sol", KEY)
        opener = Mock()
        opener.open.side_effect = generate_image.URLError("offline")
        with patch.object(generate_image, "build_opener", return_value=opener) as build:
            with self.assertRaises(generate_image.RequestError):
                generate_image._post(provider, b"{}", 1)
        proxy = next(item for item in build.call_args.args if isinstance(item, generate_image.ProxyHandler))
        self.assertEqual(proxy.proxies, {})

    def test_http_errors_do_not_retry(self) -> None:
        provider = generate_image.Provider("gpt-5.6-sol", KEY)
        with patch.object(generate_image, "resolve_provider", return_value=provider), patch.object(
            generate_image, "_post", side_effect=generate_image.RequestError(429)
        ) as post, patch.object(sys, "stdin", io.StringIO("a dog")), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = generate_image.main(["--prompt-stdin"])
        self.assertEqual(code, 4)
        self.assertEqual(post.call_count, 1)

    def test_auth_and_server_failures_submit_only_once(self) -> None:
        provider = generate_image.Provider("gpt-5.6-sol", KEY)
        for status in (0, 401, 403, 500):
            with self.subTest(status=status), patch.object(
                generate_image, "resolve_provider", return_value=provider
            ), patch.object(
                generate_image, "_post", side_effect=generate_image.RequestError(status)
            ) as post, patch.object(sys, "stdin", io.StringIO("a dog")), contextlib.redirect_stdout(
                io.StringIO()
            ), contextlib.redirect_stderr(io.StringIO()):
                code = generate_image.main(["--prompt-stdin"])
            self.assertEqual(post.call_count, 1)
            self.assertIn(code, (3, 4))


if __name__ == "__main__":
    unittest.main()
