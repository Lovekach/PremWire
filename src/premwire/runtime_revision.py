from __future__ import annotations

import hashlib
from pathlib import Path


def source_fingerprint(root: Path) -> str:
    """Return a stable fingerprint of the code loaded by local workers.

    PremWire is run directly from the checkout, so a Git commit is not enough
    to identify a dirty deployment.  Hashing the Python package and the two
    worker entry scripts makes status output able to distinguish edited files
    on disk from code that a long-running process has actually imported.
    """

    root = root.resolve()
    candidates = sorted((root / "src" / "premwire").rglob("*.py"))
    candidates.extend(
        path
        for path in (
            root / "scripts" / "premwire_entry.py",
            root / "scripts" / "premwire_supervisor.py",
        )
        if path.is_file()
    )
    digest = hashlib.sha256()
    for path in candidates:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()[:16]


__all__ = ["source_fingerprint"]
