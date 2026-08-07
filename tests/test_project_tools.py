from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import install  # noqa: E402
import package_skill  # noqa: E402
import skill_manifest  # noqa: E402


class ProjectToolTests(unittest.TestCase):
    def test_public_docs_describe_the_portdan_responses_image_route(self) -> None:
        skill = (ROOT / "skill" / "portdan-image2" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        metadata = (
            ROOT / "skill" / "portdan-image2" / "agents" / "openai.yaml"
        ).read_text(encoding="utf-8")
        public_text = "\n".join((skill, readme, metadata))

        self.assertIn("https://portdan.com/v1/responses", public_text)
        self.assertNotIn("/v1/images/generations", public_text.lower())
        self.assertIn("image_generation_call.result", public_text)
        self.assertIn("OpenAI `gpt-image-2`", skill)
        self.assertIn("Portdan as the API access and billing channel", skill)
        self.assertIn("快速、均衡还是高清", public_text)
        self.assertIn("Codex 内置生图工具不能传入", readme)
        self.assertIn("--api-key-stdin", skill)
        self.assertIn("--api-key-stdin", readme)
        self.assertIn("PORTDAN_API_KEY", public_text)
        self.assertIn("本次", public_text)
        self.assertNotIn("不要让用户提供", public_text)

    def test_dry_run_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            target = install.install(Path(temp), force=False, dry_run=True)
        self.assertEqual(target, Path(temp).resolve() / "skills" / "portdan-image2")
        self.assertFalse((Path(temp) / "skills").exists())

    def test_install_does_not_change_codex_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.toml"
            config.write_text("sentinel", encoding="utf-8")
            before = hashlib.sha256(config.read_bytes()).hexdigest()
            with contextlib.redirect_stdout(io.StringIO()):
                destination = install.install(root, force=False, dry_run=False)
            self.assertTrue((destination / "SKILL.md").is_file())
            self.assertEqual(hashlib.sha256(config.read_bytes()).hexdigest(), before)

    def test_installer_uses_same_custom_cc_switch_directory_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            home.mkdir()
            custom = Path(temp) / "custom"
            custom.mkdir()
            settings = home / ".cc-switch" / "settings.json"
            settings.parent.mkdir()
            settings.write_text(json.dumps({"codexConfigDir": str(custom)}), encoding="utf-8")
            with patch.object(Path, "home", return_value=home):
                self.assertEqual(install.default_codex_home(), custom.resolve())

    def test_package_refuses_an_existing_archive_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            package_skill.package(Path(temp), force=False)
            with self.assertRaises(RuntimeError):
                package_skill.package(Path(temp), force=False)

    def test_package_contains_only_allowlisted_skill_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            artifact = package_skill.package(Path(temp), force=False)
            with zipfile.ZipFile(artifact) as archive:
                names = set(archive.namelist())
            self.assertEqual(names, {
                "portdan-image2/LICENSE",
                "portdan-image2/SKILL.md",
                "portdan-image2/agents/openai.yaml",
                "portdan-image2/scripts/generate_image.py",
            })

    def test_manifest_reader_rejects_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target.txt"
            target.write_text("not a Skill file", encoding="utf-8")
            link = root / "link.txt"
            try:
                os.symlink(target, link)
            except (NotImplementedError, OSError) as exc:
                self.skipTest("symlink creation is unavailable: {}".format(exc))
            with self.assertRaises(RuntimeError):
                skill_manifest.read_regular_file(link)

    def test_validator_ignores_python_cache_files(self) -> None:
        cache = ROOT / "skill" / "portdan-image2" / "scripts" / "__pycache__"
        cache.mkdir(exist_ok=True)
        marker = cache / "validator-test.pyc"
        marker.write_bytes(b"interpreter cache")
        try:
            completed = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "validate_skill.py")],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))
        finally:
            marker.unlink(missing_ok=True)
            try:
                cache.rmdir()
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
