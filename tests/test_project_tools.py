from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
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
    def test_candidate_version_is_0_2_0(self) -> None:
        self.assertEqual(skill_manifest.SKILL_VERSION, "0.2.0")

    def test_public_docs_identify_the_skill_and_safe_key_contract(self) -> None:
        skill = (ROOT / "skill" / "portdan-image2" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        metadata = (
            ROOT / "skill" / "portdan-image2" / "agents" / "openai.yaml"
        ).read_text(encoding="utf-8")
        public_text = "\n".join((skill, readme, metadata))
        skill_flat = " ".join(skill.split())
        readme_flat = " ".join(readme.split())

        self.assertIn("GPT Images-compatible", skill)
        self.assertIn("does not add or promise a default model", skill_flat)
        self.assertIn("Portdan as the API access and billing channel", skill_flat)
        self.assertIn("GPT-only", skill)
        self.assertIn("/v1/images/generations", public_text)
        for canonical in ("--request-json-stdin --json", "--diagnose --json"):
            self.assertIn(canonical, skill)
            self.assertIn(canonical, readme)
        self.assertIn("portdan-image2.result.v1", public_text)
        self.assertIn("`completed`, `partial`, `error`, or `diagnose`", skill)
        self.assertIn("`stream` with `true`", skill_flat)
        self.assertIn("`response_format` with `b64_json`", skill_flat)
        self.assertIn("future fields", skill)
        self.assertIn("未知字段", readme)
        self.assertIn("`partial_images`", public_text)
        self.assertIn("`png`, `jpeg`, `webp`, or `bin`", skill)
        self.assertIn("only from the actual byte magic", skill_flat)
        self.assertIn("`.png`, `.jpeg`, `.webp`, or `.bin`", skill_flat)
        self.assertIn("`requested` is the input `n` only when", skill_flat)
        self.assertIn("positive JSON integer and not a boolean", skill_flat)
        self.assertIn("When it is null, report only `completed`", skill_flat)
        self.assertIn("`diagnostics` is null for every ordinary generation result", skill_flat)
        self.assertIn("exactly `endpoint`, `key_source`, and `output_directory`", skill_flat)
        self.assertIn("must not send a network request, create the output directory", skill_flat)
        self.assertIn("read-only solely to resolve a safe Key source code", skill_flat)
        for provider_url in (
            "https://portdan.com",
            "https://portdan.com/v1",
            "https://portdan.com/v1/images/generations",
        ):
            self.assertIn(provider_url, public_text)
        self.assertIn("Grok", public_text)
        self.assertIn("/v1/images/edits", public_text)
        self.assertIn("one HTTP POST", skill)
        self.assertIn("Never retry", skill)
        self.assertIn("1800-second network-idle timeout", skill)
        self.assertIn("and no overall deadline", skill_flat)
        self.assertIn("Do not preallocate result slots", skill_flat)
        self.assertIn("Only bytes received from the network reset", skill_flat)
        self.assertIn("Start this diagnostic runner exactly once", skill_flat)
        self.assertNotIn("900", public_text)
        self.assertIn("Do not claim that it independently verified", skill_flat)
        self.assertIn("不能独立证明", readme)
        self.assertIn("未来的 GitHub Release 制品", readme)
        self.assertIn("在发布版本时将同时提供", readme_flat)
        self.assertIn("未来的 Release 包将", readme_flat)
        for stale_contract in (
            "请选择画质",
            "请选择数量",
            "1–4 张",
            "2–4 张",
            "--count <1|2|3|4>",
            "--prompt-stdin",
            "/v1/responses",
        ):
            self.assertNotIn(stale_contract, public_text)
        self.assertIn("Codex 内置生图工具不能传入", readme)
        self.assertIn("--api-key-stdin", skill)
        self.assertIn("--api-key-stdin", readme)
        self.assertIn("PORTDAN_API_KEY", public_text)
        self.assertIn("只在当前进程内存中保留", readme)
        self.assertIn("must not export or persist it", skill)
        self.assertNotIn("当前进程中临时设置", readme)
        self.assertIn("本次", public_text)
        self.assertNotIn("不要让用户提供", public_text)
        self.assertIn("GitHub 仓库：`portdan-image2-skill`", readme)
        self.assertIn("Skill 名称：`portdan-image2`", readme)
        self.assertIn("在 Codex 中调用：`$portdan-image2`", readme)
        self.assertIn(
            "https://github.com/riven9566-source/portdan-image2-skill/"
            "tree/main/skill/portdan-image2",
            readme,
        )
        self.assertIn("portdan-image2-skill-main", readme)
        self.assertIn("这是面向支持本地 Skill 的 **Codex**", readme)
        self.assertIn("Python 3.9+", public_text)
        self.assertIn("不要把 Key 粘贴到聊天", readme)
        self.assertIn("pass its JSON value unchanged", skill_flat)
        self.assertIn("Multiple generated images may each be billed", skill)
        self.assertIn("`partial`", public_text)
        self.assertIn("`status=\"diagnose\"`", skill)
        self.assertIn("`requested=null`", skill)
        self.assertIn("`artifacts=[]`", skill)
        self.assertIn("a `diagnostics` object", skill_flat)
        self.assertIn("默认采用直连", readme)
        self.assertIn(
            'default_prompt: "使用 $portdan-image2',
            metadata,
        )
        short_description = re.search(
            r'^\s*short_description:\s*"([^"]+)"\s*$', metadata, re.MULTILINE
        )
        self.assertIsNotNone(short_description)
        self.assertGreaterEqual(len(short_description.group(1)), 25)
        self.assertLessEqual(len(short_description.group(1)), 64)
        self.assertFalse((ROOT / "SKILL.md").exists())

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
            with patch.dict(os.environ, {}, clear=True), patch.object(
                Path, "home", return_value=home
            ):
                self.assertEqual(install.default_codex_home(), custom.resolve())

    def test_installer_prefers_absolute_codex_home_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            configured = Path(temp) / "codex-home"
            with patch.dict(os.environ, {"CODEX_HOME": str(configured)}, clear=True):
                self.assertEqual(install.default_codex_home(), configured)

    def test_installer_rejects_relative_codex_home_environment(self) -> None:
        with patch.dict(os.environ, {"CODEX_HOME": "relative/codex"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "CODEX_HOME must be absolute"):
                install.default_codex_home()

    def test_installer_upgrade_requires_and_replaces_existing_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temp)
            with self.assertRaisesRegex(RuntimeError, "not installed"):
                install.install(root, force=False, dry_run=True, upgrade=True)
            destination = install.install(root, force=False, dry_run=False)
            marker = destination / "obsolete.txt"
            marker.write_text("old", encoding="utf-8")
            upgraded = install.install(root, force=False, dry_run=False, upgrade=True)
            self.assertEqual(upgraded, destination)
            self.assertFalse(marker.exists())

    def test_installer_version_flag_reports_source_version(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(ROOT / "install.py"), "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual(
            completed.stdout.decode().strip(),
            "{} {}".format(skill_manifest.SKILL_NAME, skill_manifest.SKILL_VERSION),
        )

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
            self.assertEqual(
                artifact.name,
                "portdan-image2-{}.skill".format(skill_manifest.SKILL_VERSION),
            )
            sidecar = artifact.with_name(artifact.name + ".sha256")
            self.assertTrue(sidecar.is_file())
            self.assertEqual(
                sidecar.read_text(encoding="ascii"),
                "{}  {}\n".format(hashlib.sha256(artifact.read_bytes()).hexdigest(), artifact.name),
            )

    def test_package_is_deterministic_and_matches_current_sources(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            with contextlib.redirect_stdout(io.StringIO()):
                first_artifact = package_skill.package(Path(first))
                second_artifact = package_skill.package(Path(second))
            self.assertEqual(first_artifact.read_bytes(), second_artifact.read_bytes())
            package_skill.verify_package(first_artifact)
            with zipfile.ZipFile(first_artifact) as archive:
                for relative in skill_manifest.SKILL_FILES:
                    packaged = archive.read(
                        str(Path(skill_manifest.SKILL_NAME) / relative).replace("\\", "/")
                    )
                    self.assertEqual(
                        packaged,
                        (ROOT / "skill" / skill_manifest.SKILL_NAME / relative).read_bytes(),
                    )

    def test_package_rejects_a_release_version_that_differs_from_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "does not match source version"):
                package_skill.package(Path(temp), release_version="9.9.9")

    def test_force_package_replaces_artifact_and_checksum_together(self) -> None:
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temp)
            artifact = root / "portdan-image2-{}.skill".format(
                skill_manifest.SKILL_VERSION
            )
            checksum = artifact.with_name(artifact.name + ".sha256")
            artifact.write_bytes(b"stale artifact")
            checksum.write_text("stale checksum\n", encoding="ascii")
            packaged = package_skill.package(root, force=True)
            package_skill.verify_package(packaged)
            self.assertEqual(
                checksum.read_text(encoding="ascii"),
                "{}  {}\n".format(
                    hashlib.sha256(packaged.read_bytes()).hexdigest(), packaged.name
                ),
            )

    def test_force_pair_restores_both_previous_files_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            artifact = root / "image.skill"
            checksum = root / "image.skill.sha256"
            artifact.write_bytes(b"old artifact")
            checksum.write_bytes(b"old checksum")
            new_artifact = root / "new-artifact.tmp"
            missing_checksum = root / "missing-checksum.tmp"
            new_artifact.write_bytes(b"new artifact")
            with self.assertRaises(OSError):
                package_skill._replace_package_pair(
                    [
                        (new_artifact, artifact),
                        (missing_checksum, checksum),
                    ]
                )
            self.assertEqual(artifact.read_bytes(), b"old artifact")
            self.assertEqual(checksum.read_bytes(), b"old checksum")
            self.assertEqual(list(root.glob("*.backup-*")), [])

    def test_repository_does_not_keep_the_legacy_dist_archive(self) -> None:
        self.assertFalse((ROOT / "dist" / "portdan-image2.skill").exists())

    def test_release_workflow_builds_versioned_source_exact_artifacts(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "release-skill.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("tags:", workflow)
        self.assertIn("SKILL_VERSION", workflow)
        self.assertIn("--release-version", workflow)
        self.assertIn("sha256sum --check", workflow)
        self.assertIn("gh release create", workflow)
        self.assertIn(
            "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
            workflow,
        )
        self.assertIn("ref: ${{ github.sha }}", workflow)
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("git show-ref --verify --quiet refs/remotes/origin/main", workflow)
        self.assertIn("git merge-base --is-ancestor", workflow)
        self.assertLess(
            workflow.index("Verify the tagged commit belongs to main"),
            workflow.index("Use Python 3.9"),
        )
        self.assertLess(
            workflow.index("git merge-base --is-ancestor"),
            workflow.index("python -c 'import skill_manifest"),
        )
        self.assertNotIn("softprops/action-gh-release", workflow)

    def test_ci_runs_offline_contracts_on_supported_os_and_python_matrix(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        for runner in ("ubuntu-latest", "macos-latest", "windows-latest"):
            self.assertIn(runner, workflow)
        self.assertIn('- "3.9"', workflow)
        self.assertIn('- "3.x"', workflow)
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("python -m unittest discover -s tests -v", workflow)
        self.assertIn("python tools/validate_skill.py", workflow)
        self.assertIn("python -m py_compile", workflow)
        self.assertIn("python package_skill.py --output-dir build/ci", workflow)
        self.assertNotIn("secrets.", workflow.lower())
        self.assertNotIn("portdan.com", workflow.lower())
        uses = [
            line.strip().split("@", 1)[1].split()[0]
            for line in workflow.splitlines()
            if line.strip().startswith("uses:")
        ]
        self.assertGreaterEqual(len(uses), 2)
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{40}", value) for value in uses))

    def test_skill_behavior_evals_are_real_and_bounded(self) -> None:
        payload = json.loads((ROOT / "evals" / "evals.json").read_text(encoding="utf-8"))
        self.assertEqual(payload.get("skill_name"), skill_manifest.SKILL_NAME)
        evals = payload.get("evals")
        self.assertIsInstance(evals, list)
        self.assertEqual(len(evals), 6)
        identifiers = []
        for item in evals:
            self.assertIsInstance(item, dict)
            identifiers.append(item.get("id"))
            self.assertEqual(
                set(item),
                {"id", "prompt", "expected_output", "files", "expectations"},
            )
            self.assertIsInstance(item.get("id"), int)
            self.assertTrue(str(item.get("prompt", "")).strip())
            self.assertTrue(str(item.get("expected_output", "")).strip())
            self.assertIsInstance(item.get("files"), list)
            expectations = item.get("expectations")
            self.assertIsInstance(expectations, list)
            self.assertGreaterEqual(len(expectations), 3)
            self.assertTrue(all(isinstance(entry, str) and entry.strip() for entry in expectations))
        self.assertEqual(len(identifiers), len(set(identifiers)))
        all_prompts = "\n".join(item["prompt"] for item in evals)
        all_eval_text = json.dumps(evals, ensure_ascii=False)
        self.assertIn("$portdan-image2", all_prompts)
        self.assertNotIn("sk-", all_prompts)
        self.assertIn("--request-json-stdin --json", all_eval_text)
        self.assertIn("future_render_mode", all_eval_text)
        self.assertIn("gpt-image-future-preview", all_eval_text)
        self.assertIn("n=100", all_eval_text)
        self.assertIn("partial_images=true", all_eval_text)
        self.assertIn("quality=null", all_eval_text)
        self.assertIn('"modes":["draft",null]', evals[1]["prompt"])
        self.assertIn("没有找到可用的 Portdan Key", all_prompts)
        self.assertIn("https://portdan.com/v1/images/generations", all_prompts)
        self.assertIn("session ID", all_prompts)
        self.assertIn("status=partial", all_prompts)
        self.assertIn("requested=null", all_prompts)
        self.assertIn(".bin", all_eval_text)
        self.assertIn("无 overall deadline", all_eval_text)
        self.assertIn("1800", all_eval_text)
        self.assertIn("--diagnose --json", all_eval_text)
        self.assertIn("`diagnostics` 恰好包含", all_eval_text)
        self.assertNotIn("请选择画质", all_eval_text)
        self.assertNotIn("1–4", all_eval_text)
        self.assertNotIn("2–4", all_eval_text)

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
        marker = cache / "validator-test-{}.pyc".format(os.getpid())
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
