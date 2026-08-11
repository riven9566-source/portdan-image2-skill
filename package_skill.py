#!/usr/bin/env python3
"""Build a deterministic .skill archive from the allowlisted files."""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

from skill_manifest import (
    SKILL_FILES,
    SKILL_NAME,
    SKILL_VERSION,
    read_regular_file,
    validate_skill_version,
)


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "skill" / SKILL_NAME


def _replace_package_pair(replacements: list[tuple[Path, Path]]) -> None:
    """Replace an artifact and checksum together, restoring both on failure."""
    backups: list[tuple[Path, Path]] = []
    installed: list[Path] = []
    token = secrets.token_hex(6)
    completed = False
    try:
        for _temporary, destination in replacements:
            if destination.exists():
                backup = destination.with_name(destination.name + ".backup-" + token)
                destination.replace(backup)
                backups.append((destination, backup))
        for temporary, destination in replacements:
            os.replace(temporary, destination)
            installed.append(destination)
        completed = True
    except Exception:
        for destination in installed:
            if destination.exists():
                destination.unlink()
        for destination, backup in reversed(backups):
            if backup.exists():
                backup.replace(destination)
        raise
    finally:
        if completed:
            for _destination, backup in backups:
                if backup.exists():
                    backup.unlink()


def verify_package(artifact: Path) -> None:
    """Prove that an archive contains exactly the current allowlisted sources."""
    expected = {
        str(Path(SKILL_NAME) / relative).replace("\\", "/"): read_regular_file(
            SOURCE / relative
        )
        for relative in SKILL_FILES
    }
    try:
        with zipfile.ZipFile(artifact) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or set(names) != set(expected):
                raise RuntimeError("package file allowlist mismatch")
            for name, source in expected.items():
                if archive.read(name) != source:
                    raise RuntimeError("package differs from Skill source: " + name)
    except zipfile.BadZipFile as exc:
        raise RuntimeError("package is not a valid .skill archive") from exc


def package(
    output_dir: Path, force: bool = False, release_version: Optional[str] = None
) -> Path:
    version = validate_skill_version(release_version or SKILL_VERSION)
    if version != SKILL_VERSION:
        raise RuntimeError(
            "release version {} does not match source version {}".format(
                version, SKILL_VERSION
            )
        )
    subprocess.run([sys.executable, str(ROOT / "tools" / "validate_skill.py")], check=True)
    files = [(relative, read_regular_file(SOURCE / relative)) for relative in SKILL_FILES]
    output_dir = output_dir.expanduser()
    if output_dir.exists() and (output_dir.is_symlink() or not output_dir.is_dir()):
        raise RuntimeError("package output directory is invalid")
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / (SKILL_NAME + "-" + version + ".skill")
    checksum = destination.with_name(destination.name + ".sha256")
    for path in (destination, checksum):
        if path.is_symlink():
            raise RuntimeError("refusing a symlinked package destination")
        if path.exists() and not force:
            raise RuntimeError("package already exists; use --force to replace it")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=destination.name + ".", suffix=".tmp", dir=str(output_dir)
    )
    temporary = Path(temporary_name)
    checksum_descriptor = -1
    checksum_temporary: Optional[Path] = None
    destination_created = False
    try:
        with os.fdopen(descriptor, "w+b") as handle:
            descriptor = -1
            # Store without deflate so identical sources produce identical bytes even
            # when builders use different zlib versions.
            with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_STORED) as archive:
                for relative, data in files:
                    info = zipfile.ZipInfo(str(Path(SKILL_NAME) / relative).replace("\\", "/"))
                    info.date_time = (2026, 1, 1, 0, 0, 0)
                    info.create_system = 3
                    info.external_attr = 0o100644 << 16
                    archive.writestr(info, data, compress_type=zipfile.ZIP_STORED)
            handle.flush()
            os.fsync(handle.fileno())
        verify_package(temporary)
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        checksum_text = "{}  {}\n".format(digest, destination.name)
        checksum_descriptor, checksum_name = tempfile.mkstemp(
            prefix=checksum.name + ".", suffix=".tmp", dir=str(output_dir)
        )
        checksum_temporary = Path(checksum_name)
        with os.fdopen(checksum_descriptor, "w", encoding="ascii", newline="\n") as handle:
            checksum_descriptor = -1
            handle.write(checksum_text)
            handle.flush()
            os.fsync(handle.fileno())
        if force:
            assert checksum_temporary is not None
            _replace_package_pair(
                [(temporary, destination), (checksum_temporary, checksum)]
            )
        else:
            try:
                os.link(str(temporary), str(destination), follow_symlinks=False)
                destination_created = True
                os.link(str(checksum_temporary), str(checksum), follow_symlinks=False)
            except FileExistsError:
                if destination_created:
                    destination.unlink()
                raise RuntimeError("package already exists; use --force to replace it") from None
            except Exception:
                if destination_created and destination.exists():
                    destination.unlink()
                raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if checksum_descriptor >= 0:
            os.close(checksum_descriptor)
        if temporary.exists():
            temporary.unlink()
        if checksum_temporary is not None and checksum_temporary.exists():
            checksum_temporary.unlink()
    print(destination.absolute())
    print(checksum.absolute())
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description="Package Portdan Image2")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument(
        "--release-version",
        default=None,
        help="must match the source MAJOR.MINOR.PATCH version",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        package(args.output_dir, args.force, args.release_version)
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print("Package error: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
