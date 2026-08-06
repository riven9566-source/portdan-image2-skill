#!/usr/bin/env python3
"""Build a deterministic .skill archive from the allowlisted files."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from skill_manifest import SKILL_FILES, SKILL_NAME, read_regular_file


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "skill" / SKILL_NAME


def package(output_dir: Path, force: bool = False) -> Path:
    subprocess.run([sys.executable, str(ROOT / "tools" / "validate_skill.py")], check=True)
    files = [(relative, read_regular_file(SOURCE / relative)) for relative in SKILL_FILES]
    output_dir = output_dir.expanduser()
    if output_dir.exists() and (output_dir.is_symlink() or not output_dir.is_dir()):
        raise RuntimeError("package output directory is invalid")
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / (SKILL_NAME + ".skill")
    if destination.is_symlink():
        raise RuntimeError("refusing a symlinked package destination")
    if destination.exists() and not force:
        raise RuntimeError("package already exists; use --force to replace it")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=destination.name + ".", suffix=".tmp", dir=str(output_dir)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w+b") as handle:
            descriptor = -1
            with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                for relative, data in files:
                    info = zipfile.ZipInfo(str(Path(SKILL_NAME) / relative).replace("\\", "/"))
                    info.date_time = (2026, 1, 1, 0, 0, 0)
                    info.create_system = 3
                    info.external_attr = 0o100644 << 16
                    archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
            handle.flush()
            os.fsync(handle.fileno())
        if force:
            os.replace(temporary, destination)
        else:
            try:
                os.link(str(temporary), str(destination), follow_symlinks=False)
            except FileExistsError:
                raise RuntimeError("package already exists; use --force to replace it") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()
    print(destination.absolute())
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description="Package Portdan Image2")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        package(args.output_dir, args.force)
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print("Package error: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
