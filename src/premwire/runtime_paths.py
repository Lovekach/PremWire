from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def resolve_runtime_root(environ: Mapping[str, str] | None = None) -> Path:
    """Return the machine-local PremWire runtime root.

    PREMWIRE_RUNTIME is authoritative.  Windows falls back to a directory in
    the current user's profile so a repository checkout never becomes the
    implicit production runtime.
    """

    env = os.environ if environ is None else environ
    configured = str(env.get("PREMWIRE_RUNTIME", "")).strip()
    if configured:
        return Path(configured).expanduser().resolve()

    # Public development must not attach to an operator's pre-existing runtime.
    return (Path.cwd() / ".runtime").resolve()


def runtime_path(*parts: str, environ: Mapping[str, str] | None = None) -> Path:
    return resolve_runtime_root(environ) / Path(*parts)


__all__ = ["resolve_runtime_root", "runtime_path"]
