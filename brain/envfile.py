"""Small, dependency-free loader for the board-local App Lab environment.

App Lab launches Python directly, so the preserved ``.env`` file is not sourced
by a shell. Values are never logged and existing process environment wins.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import MutableMapping, Optional
from urllib.parse import urlsplit, urlunsplit

_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_env(path: Path | str,
             environ: Optional[MutableMapping[str, str]] = None) -> int:
    """Load simple KEY=VALUE lines without expanding commands or variables."""
    target = os.environ if environ is None else environ
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, PermissionError, OSError):
        return 0

    loaded = 0
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not _KEY.fullmatch(key) or key in target:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        target[key] = value
        loaded += 1
    return loaded


def route_loopback_url(url: str, gateway_host: str) -> str:
    """Route a host-loopback HTTP URL through an App Lab host gateway."""
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or not gateway_host):
        return url
    port = f":{parsed.port}" if parsed.port is not None else ""
    return urlunsplit(parsed._replace(netloc=f"{gateway_host}{port}"))
