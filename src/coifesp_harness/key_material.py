from __future__ import annotations

import base64
import binascii
from .config import ConfigurationError, SecretKeyConfig, SecretValue, Settings


def configured_keys(
    *,
    active_key_id: str | None,
    versioned: tuple[SecretKeyConfig, ...],
    legacy: SecretValue | None,
    legacy_name: str,
    decode_base64url: bool = False,
) -> tuple[str, dict[str, bytes]]:
    """Resolve an active key plus verification/decryption keys without leaking material."""
    if not active_key_id:
        raise ConfigurationError(
            f"{legacy_name.replace('_SIGNING_KEY', '_KEY_ID').replace('_MASTER_KEY', '_KEY_ID')} is required"
        )
    if versioned:
        values = {item.key_id: item.material.reveal() for item in versioned}
    elif legacy is not None:
        values = {active_key_id: legacy.reveal()}
    else:
        raise ConfigurationError(f"{legacy_name} or its versioned keyring is required")
    keys = {
        key_id: _decode(value, name=legacy_name, base64url=decode_base64url)
        for key_id, value in values.items()
    }
    if active_key_id not in keys:
        raise ConfigurationError(f"active key {active_key_id!r} is unavailable")
    return active_key_id, keys


def memory_keys_from_settings(settings: Settings) -> tuple[str, dict[str, bytes]]:
    settings.validate(require_memory=True)
    return configured_keys(
        active_key_id=settings.memory_key_id,
        versioned=settings.memory_keys,
        legacy=settings.memory_master_key,
        legacy_name="COIFESP_MEMORY_MASTER_KEY",
        decode_base64url=True,
    )


def _decode(value: str, *, name: str, base64url: bool) -> bytes:
    if not base64url:
        encoded = value.encode("utf-8")
        if len(encoded) < 32:
            raise ConfigurationError(f"{name} must contain at least 32 bytes")
        return encoded
    try:
        decoded = base64.b64decode(value, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ConfigurationError(f"{name} must be Base64URL-encoded 32 bytes") from exc
    if len(decoded) != 32:
        raise ConfigurationError(f"{name} must be Base64URL-encoded 32 bytes")
    return decoded
