from __future__ import annotations

import base64
import ctypes
import json
import os
import re
from ctypes import wintypes
from pathlib import Path


_TOKEN_RE = re.compile(r"^\d{5,20}:[A-Za-z0-9_-]{30,}$")
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class SecretStoreError(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def valid_telegram_bot_token(value: str) -> bool:
    return bool(_TOKEN_RE.fullmatch(value.strip()))


def _input_blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    blob = _DataBlob(
        len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    )
    return blob, buffer


def _crypt32() -> tuple[ctypes.WinDLL, ctypes.WinDLL]:
    if os.name != "nt":
        raise SecretStoreError(
            "Encrypted local secret storage is currently available only on Windows"
        )
    return (
        ctypes.WinDLL("crypt32", use_last_error=True),
        ctypes.WinDLL("kernel32", use_last_error=True),
    )


def protect_secret(value: str) -> bytes:
    crypt32, kernel32 = _crypt32()
    source, source_buffer = _input_blob(value.encode("utf-8"))
    output = _DataBlob()
    result = crypt32.CryptProtectData(
        ctypes.byref(source),
        "PremWire local secret",
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output),
    )
    del source_buffer
    if not result:
        raise SecretStoreError(
            f"Windows could not encrypt the secret: {ctypes.get_last_error()}"
        )
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)


def unprotect_secret(payload: bytes) -> str:
    crypt32, kernel32 = _crypt32()
    source, source_buffer = _input_blob(payload)
    output = _DataBlob()
    result = crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output),
    )
    del source_buffer
    if not result:
        raise SecretStoreError(
            f"Windows could not decrypt the secret: {ctypes.get_last_error()}"
        )
    try:
        return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(output.pbData)


def _read_payload(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SecretStoreError(f"Could not read encrypted secret store: {path}") from exc
    if not isinstance(payload, dict):
        raise SecretStoreError("Encrypted secret store has an invalid format")
    return {str(key): str(value) for key, value in payload.items()}


def set_secret(path: Path, key: str, value: str) -> None:
    payload = _read_payload(path)
    payload[key] = base64.b64encode(protect_secret(value)).decode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def load_secrets(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, encoded in _read_payload(path).items():
        try:
            result[key] = unprotect_secret(base64.b64decode(encoded))
        except (ValueError, SecretStoreError) as exc:
            raise SecretStoreError(f"Could not decrypt secret {key}") from exc
    return result


def secret_store_status(path: Path) -> str:
    """Probe DPAPI compatibility without exposing decrypted values."""

    if not path.exists():
        return "MISSING"
    try:
        values = load_secrets(path)
    except (ValueError, SecretStoreError):
        return "SECRETS_REAUTH_REQUIRED"
    return "READY" if values else "EMPTY"
