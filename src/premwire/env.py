from __future__ import annotations

import os
from pathlib import Path

from .secure_store import SecretStoreError, load_secrets


def load_env(path: Path) -> dict[str, str]:
    try:
        values = load_secrets(path.parent / "data" / "secrets.json")
    except SecretStoreError:
        # DPAPI secrets are tied to the Windows user session. Commands running in
        # a sandbox can still use .env or process environment variables safely.
        values = {}
    if path.exists():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    for key, value in os.environ.items():
        if key.startswith(("TELEGRAM_", "X_", "VK_", "EDITOR_")):
            values[key] = value
    return values
