"""The exact files that are allowed in the public Skill package."""

from __future__ import annotations

import os
import stat
from pathlib import Path


SKILL_NAME = "portdan-image2"
SKILL_FILES = (
    Path("LICENSE"),
    Path("SKILL.md"),
    Path("agents/openai.yaml"),
    Path("scripts/generate_image.py"),
)

MAX_SKILL_FILE_BYTES = 512 * 1024


def _is_link_like(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def read_regular_file(path: Path, limit: int = MAX_SKILL_FILE_BYTES) -> bytes:
    """Read one allowlisted source file without following a link or reopening it."""
    descriptor = -1
    data = b""
    try:
        before = path.lstat()
    except OSError as exc:
        raise RuntimeError("Skill source file is unavailable: " + str(path)) from exc
    if _is_link_like(before) or not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise RuntimeError("Skill source file is invalid: " + str(path))
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(str(path), flags)
        opened = os.fstat(descriptor)
        if (
            _is_link_like(opened)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_size > limit
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise RuntimeError("Skill source file changed unexpectedly: " + str(path))
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            data = handle.read(limit + 1)
    except OSError as exc:
        raise RuntimeError("Skill source file is unavailable: " + str(path)) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(data) > limit:
        raise RuntimeError("Skill source file is too large: " + str(path))
    return data
