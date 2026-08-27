from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..config import Settings
from ..errors import IntegrityError
from ..key_material import memory_keys_from_settings


@dataclass(frozen=True, slots=True)
class EncryptedToolPayload:
    ciphertext: bytes
    nonce: bytes
    fingerprint: str
    key_id: str


class ToolJobKeyring:
    """Domain-separated tenant encryption for tool arguments and results."""

    def __init__(
        self,
        *,
        master_key: bytes | None = None,
        key_id: str | None = None,
        active_key_id: str | None = None,
        decryption_keys: dict[str, bytes] | None = None,
    ) -> None:
        active = active_key_id or key_id
        keys = dict(decryption_keys or ({active: master_key} if active and master_key else {}))
        if not active or active not in keys or any(len(value) != 32 for value in keys.values()):
            raise ValueError("tool job key material is invalid")
        self._keys = {name: bytes(value) for name, value in keys.items()}
        self.key_id = active

    @classmethod
    def from_settings(cls, settings: Settings) -> "ToolJobKeyring":
        active, keys = memory_keys_from_settings(settings)
        return cls(active_key_id=active, decryption_keys=keys)

    def encrypt(
        self, *, tenant_id: str, job_id: str, purpose: str, value: Any
    ) -> EncryptedToolPayload:
        plaintext = self._canonical(value)
        if len(plaintext) > 1_048_576:
            raise ValueError("tool payload exceeds the 1 MiB limit")
        key = self._derive(tenant_id, self.key_id)
        nonce = os.urandom(12)
        return EncryptedToolPayload(
            AESGCM(key).encrypt(
                nonce, plaintext, self._aad(tenant_id, job_id, purpose, self.key_id)
            ),
            nonce,
            hmac.new(key, plaintext, hashlib.sha256).hexdigest(),
            self.key_id,
        )

    def decrypt(
        self,
        *,
        tenant_id: str,
        job_id: str,
        purpose: str,
        ciphertext: bytes,
        nonce: bytes,
        fingerprint: str,
        key_id: str,
    ) -> Any:
        if key_id not in self._keys:
            raise IntegrityError("tool payload key version is unavailable")
        key = self._derive(tenant_id, key_id)
        try:
            plaintext = AESGCM(key).decrypt(
                nonce, ciphertext, self._aad(tenant_id, job_id, purpose, key_id)
            )
        except InvalidTag as exc:
            raise IntegrityError("tool payload authentication failed") from exc
        if not hmac.compare_digest(
            hmac.new(key, plaintext, hashlib.sha256).hexdigest(), fingerprint
        ):
            raise IntegrityError("tool payload fingerprint is invalid")
        try:
            return json.loads(plaintext)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IntegrityError("tool payload plaintext is invalid") from exc

    def digest(self, *, tenant_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
        return hmac.new(
            self._derive(tenant_id, self.key_id),
            self._canonical({"tool_name": tool_name, "arguments": arguments}),
            hashlib.sha256,
        ).hexdigest()

    def digests(
        self, *, tenant_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> frozenset[str]:
        canonical = self._canonical({"tool_name": tool_name, "arguments": arguments})
        return frozenset(
            hmac.new(self._derive(tenant_id, key_id), canonical, hashlib.sha256).hexdigest()
            for key_id in self._keys
        )

    def _derive(self, tenant_id: str, key_id: str) -> bytes:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=hashlib.sha256(f"coifesp-tool-job:{key_id}".encode()).digest(),
            info=f"tenant:{tenant_id}:tool-job:v1".encode(),
        ).derive(self._keys[key_id])

    @staticmethod
    def _aad(tenant_id: str, job_id: str, purpose: str, key_id: str) -> bytes:
        return ToolJobKeyring._canonical(
            {
                "schema": "coifesp.tool-payload.v1",
                "tenant_id": tenant_id,
                "job_id": job_id,
                "purpose": purpose,
                "key_id": key_id,
            }
        )

    @staticmethod
    def _canonical(value: Any) -> bytes:
        try:
            return json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        except (TypeError, ValueError) as exc:
            raise ValueError("tool payload must be canonical JSON") from exc
