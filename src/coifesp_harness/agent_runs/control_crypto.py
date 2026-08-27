from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..config import Settings
from ..errors import IntegrityError
from ..key_material import memory_keys_from_settings


@dataclass(frozen=True, slots=True)
class EncryptedControlContent:
    ciphertext: bytes
    nonce: bytes
    fingerprint: str
    key_id: str


class AgentControlKeyring:
    """Domain-separated encryption for steering and follow-up content."""

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
            raise ValueError("control key material is invalid")
        self._keys = {name: bytes(value) for name, value in keys.items()}
        self.key_id = active

    @classmethod
    def from_settings(cls, settings: Settings) -> "AgentControlKeyring":
        active, keys = memory_keys_from_settings(settings)
        return cls(active_key_id=active, decryption_keys=keys)

    def encrypt(
        self,
        *,
        tenant_id: str,
        run_id: str,
        sequence: int,
        command_id: str,
        command_type: str,
        content: str,
    ) -> EncryptedControlContent:
        plaintext = content.encode("utf-8")
        if not plaintext or len(plaintext) > 65_536:
            raise ValueError("control content must contain 1 to 65536 UTF-8 bytes")
        key = self._derive(tenant_id, self.key_id)
        nonce = os.urandom(12)
        aad = self._aad(tenant_id, run_id, sequence, command_id, command_type, self.key_id)
        return EncryptedControlContent(
            ciphertext=AESGCM(key).encrypt(nonce, plaintext, aad),
            nonce=nonce,
            fingerprint=hmac.new(key, plaintext, hashlib.sha256).hexdigest(),
            key_id=self.key_id,
        )

    def decrypt(
        self,
        *,
        tenant_id: str,
        run_id: str,
        sequence: int,
        command_id: str,
        command_type: str,
        ciphertext: bytes,
        nonce: bytes,
        fingerprint: str,
        key_id: str,
    ) -> str:
        if key_id not in self._keys:
            raise IntegrityError("control content key version is unavailable")
        key = self._derive(tenant_id, key_id)
        try:
            plaintext = AESGCM(key).decrypt(
                nonce,
                ciphertext,
                self._aad(tenant_id, run_id, sequence, command_id, command_type, key_id),
            )
        except InvalidTag as exc:
            raise IntegrityError("control content authentication failed") from exc
        if not hmac.compare_digest(
            hmac.new(key, plaintext, hashlib.sha256).hexdigest(),
            fingerprint,
        ):
            raise IntegrityError("control content fingerprint is invalid")
        try:
            content = plaintext.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise IntegrityError("control content is not UTF-8") from exc
        if not content or len(plaintext) > 65_536:
            raise IntegrityError("control content bounds are invalid")
        return content

    def request_digest(
        self,
        *,
        tenant_id: str,
        run_id: str,
        command_id: str,
        command_type: str,
        content: str,
    ) -> str:
        canonical = json.dumps(
            {
                "run_id": run_id,
                "command_id": command_id,
                "command_type": command_type,
                "content": content,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(self._derive(tenant_id, self.key_id), canonical, hashlib.sha256).hexdigest()

    def request_digests(
        self,
        *,
        tenant_id: str,
        run_id: str,
        command_id: str,
        command_type: str,
        content: str,
    ) -> frozenset[str]:
        canonical = json.dumps(
            {
                "run_id": run_id,
                "command_id": command_id,
                "command_type": command_type,
                "content": content,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
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
            salt=hashlib.sha256(f"coifesp-agent-control:{key_id}".encode()).digest(),
            info=f"tenant:{tenant_id}:agent-control:v1".encode(),
        ).derive(self._keys[key_id])

    @staticmethod
    def _aad(
        tenant_id: str,
        run_id: str,
        sequence: int,
        command_id: str,
        command_type: str,
        key_id: str,
    ) -> bytes:
        return json.dumps(
            {
                "schema": "coifesp.agent-control.ciphertext.v1",
                "tenant_id": tenant_id,
                "run_id": run_id,
                "sequence": sequence,
                "command_id": command_id,
                "command_type": command_type,
                "key_id": key_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
