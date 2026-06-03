"""
jybot.envload -- tiny, dependency-free ``.env`` loader.

We deliberately do NOT require ``python-dotenv`` so the bot can boot on a clean
machine. If ``python-dotenv`` is installed it is used (it handles a few edge
cases nicely); otherwise we fall back to this minimal parser, which covers the
``KEY=VALUE`` / ``export KEY=VALUE`` / quoted-value / ``#`` comment cases used
in ``.env.example``.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def _parse_line(line: str):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export "):].strip()
    if "=" not in line:
        return None
    key, _, value = line.partition("=")
    key = key.strip()
    value = value.strip()
    # Strip inline comments only for unquoted values.
    if value and value[0] in ("'", '"'):
        quote = value[0]
        end = value.find(quote, 1)
        if end != -1:
            value = value[1:end]
        else:
            value = value[1:]
    else:
        # Drop trailing inline comment (space + #).
        hash_idx = value.find(" #")
        if hash_idx != -1:
            value = value[:hash_idx]
        value = value.strip()
    if not key:
        return None
    return key, value


def load_dotenv(path: Optional[str] = None, override: bool = False) -> bool:
    """Load environment variables from a ``.env`` file.

    Returns True if a file was found and parsed. Existing ``os.environ`` values
    are preserved unless ``override=True``.
    """
    # Prefer python-dotenv when available.
    try:
        from dotenv import load_dotenv as _real_load_dotenv  # type: ignore

        env_path = path or str(_find_env_file())
        if env_path and os.path.exists(env_path):
            _real_load_dotenv(env_path, override=override)
            return True
        return False
    except Exception:
        pass

    env_file = Path(path) if path else _find_env_file()
    if not env_file or not env_file.exists():
        return False

    try:
        text = env_file.read_text(encoding="utf-8")
    except Exception:
        text = env_file.read_text(errors="ignore")

    for raw in text.splitlines():
        parsed = _parse_line(raw)
        if not parsed:
            continue
        key, value = parsed
        if override or key not in os.environ:
            os.environ[key] = value
    return True


def _find_env_file() -> Optional[Path]:
    """Walk up from the project root looking for a ``.env`` file."""
    here = Path(__file__).resolve().parent.parent  # project root
    candidate = here / ".env"
    if candidate.exists():
        return candidate
    # also try current working directory
    cwd_candidate = Path.cwd() / ".env"
    if cwd_candidate.exists():
        return cwd_candidate
    return candidate  # return default path even if missing
