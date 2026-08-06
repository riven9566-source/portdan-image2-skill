#!/usr/bin/env python3
"""Validate the public Skill allowlist and frontmatter."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from skill_manifest import SKILL_FILES, SKILL_NAME, _is_link_like, read_regular_file
SKILL_ROOT = ROOT / "skill" / SKILL_NAME


def main() -> int:
    if not SKILL_ROOT.is_dir() or SKILL_ROOT.is_symlink():
        raise RuntimeError("Skill directory is invalid")
    expected = {str(path).replace("\\", "/") for path in SKILL_FILES}
    entries = list(SKILL_ROOT.rglob("*"))
    if any(_is_link_like(path.lstat()) for path in entries):
        raise RuntimeError("Skill must not contain symlinks")
    actual = {
        str(path.relative_to(SKILL_ROOT)).replace("\\", "/")
        for path in entries
        if path.is_file()
    }
    if actual != expected:
        raise RuntimeError("Skill file allowlist mismatch")
    for relative in SKILL_FILES:
        read_regular_file(SKILL_ROOT / relative)
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    if not re.search(r"^name:\s*" + re.escape(SKILL_NAME) + r"\s*$", skill, re.M):
        raise RuntimeError("SKILL.md name is invalid")
    if not re.search(r"^description:\s*", skill, re.M):
        raise RuntimeError("SKILL.md description is missing")
    if (SKILL_ROOT / "LICENSE").read_bytes() != (ROOT / "LICENSE").read_bytes():
        raise RuntimeError("Skill LICENSE must match the repository LICENSE")
    print("Skill validation OK: {}".format(SKILL_ROOT))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as exc:
        print("Skill validation error: {}".format(exc), file=sys.stderr)
        raise SystemExit(1)
