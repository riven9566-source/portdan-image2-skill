#!/usr/bin/env python3
"""Install the allowlisted Portdan Skill without touching Codex settings."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import stat
import tempfile
from pathlib import Path

from skill_manifest import SKILL_FILES, SKILL_NAME, _is_link_like, read_regular_file


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "skill" / SKILL_NAME
MAX_SETTINGS_BYTES = 512 * 1024


def _read_settings(path: Path) -> dict:
    try:
        raw = read_regular_file(path, MAX_SETTINGS_BYTES)
        payload = json.loads(raw.decode("utf-8-sig"))
    except (RuntimeError, UnicodeError, ValueError):
        raise RuntimeError("CC Switch settings.json is invalid") from None
    if not isinstance(payload, dict):
        raise RuntimeError("CC Switch settings.json is invalid")
    return payload


def _existing_real_directory(path: Path) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise RuntimeError("CC Switch codexConfigDir is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or _is_link_like(info):
        raise RuntimeError("CC Switch codexConfigDir is invalid")
    return path.resolve()


def default_codex_home() -> Path:
    home = Path.home()
    settings = home / ".cc-switch" / "settings.json"
    if settings.exists() or settings.is_symlink():
        custom = _read_settings(settings).get("codexConfigDir")
        if custom is not None:
            if not isinstance(custom, str) or not Path(custom).expanduser().is_absolute():
                raise RuntimeError("CC Switch codexConfigDir must be absolute")
            if not custom.strip():
                raise RuntimeError("CC Switch codexConfigDir is invalid")
            return _existing_real_directory(Path(custom.strip()).expanduser())
    return home / ".codex"


def approved_sources() -> list[tuple[bytes, Path]]:
    try:
        source_info = SOURCE.lstat()
    except OSError as exc:
        raise RuntimeError("Skill source directory is invalid") from exc
    if _is_link_like(source_info) or not stat.S_ISDIR(source_info.st_mode):
        raise RuntimeError("Skill source directory is invalid")
    result: list[tuple[bytes, Path]] = []
    for relative in SKILL_FILES:
        source = SOURCE / relative
        result.append((read_regular_file(source), relative))
    return result


def _write_staging_file(path: Path, data: bytes) -> None:
    descriptor = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(str(path), flags, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("could not write Skill file")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def install(target: Path, force: bool, dry_run: bool) -> Path:
    files = approved_sources()
    target = target.expanduser().resolve()
    if target == Path(target.anchor):
        raise RuntimeError("refusing to install into a filesystem root")
    skills = target / "skills"
    if skills.exists() and (skills.is_symlink() or not skills.is_dir()):
        raise RuntimeError("Codex skills directory is invalid")
    destination = skills / SKILL_NAME
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise RuntimeError("Skill destination is invalid")
    if dry_run:
        print("Would install {} files to {}".format(len(files), destination))
        return destination
    skills.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not force:
        raise RuntimeError("destination already exists; use --force to replace this Skill")
    staging_parent = Path(tempfile.mkdtemp(prefix=SKILL_NAME + "-", dir=str(skills)))
    staging = staging_parent / SKILL_NAME
    backup = destination.with_name(destination.name + ".backup-" + secrets.token_hex(6))
    try:
        for data, relative in files:
            target_file = staging / relative
            target_file.parent.mkdir(parents=True, exist_ok=True)
            _write_staging_file(target_file, data)
        if destination.exists():
            destination.replace(backup)
        try:
            staging.replace(destination)
        except Exception:
            if backup.exists() and not destination.exists():
                backup.replace(destination)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)
    print("Installed {} to {}".format(SKILL_NAME, destination))
    print("Restart Codex, then use $portdan-image2.")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description="Install Portdan Image2")
    parser.add_argument("--codex-home", type=Path, default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        install(args.codex_home or default_codex_home(), args.force, args.dry_run)
        return 0
    except (OSError, RuntimeError) as exc:
        print("Install error: {}".format(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
