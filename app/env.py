"""Minimal, dependency-free ``.env`` loader.

Only ``KEY=VALUE`` lines are read. Values already present in the real
environment win, so production secrets injected by the platform are never
overridden by a stray file. Nothing in ``.env`` is ever executed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()  # strip inline comments on unquoted values
        values[key] = value
    return values


def load_env_file(path: str | Path = ".env") -> dict[str, str]:
    """Load ``path`` into ``os.environ`` without overriding existing variables."""
    file = Path(path)
    if not file.is_file():
        return {}
    loaded = parse_env(file.read_text(encoding="utf-8"))
    for key, value in loaded.items():
        os.environ.setdefault(key, value)
    return loaded
