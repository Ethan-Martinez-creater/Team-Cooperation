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
class EncryptedCheckpoint:
    ciphertext: bytes
    nonce: bytes
    fingerprint: str
    key_id: str


class AgentCheckpointKeyring:
    """Domain-separated tenant encryption for durable Agent checkpoints."""

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
            raise ValueError("checkpoint key material is invalid")
        self._keys = {name: bytes(value) for name, value in keys.items()}
        self.key_id = active

    @classmethod
    def from_settings(cls, settings: Settings) -> "AgentCheckpointKeyring":
        active, keys = memory_keys_from_settings(settings)
        return cls(active_key_id=active, decryption_keys=keys)

    def encrypt(
        self,
        *,
        tenant_id: str,
        run_id: str,
        version: int,
        checkpoint: dict,
    ) -> EncryptedCheckpoint:
        plaintext = self._canonical(checkpoint)
        if len(plaintext) > 2_097_152:
            raise ValueError("agent checkpoint exceeds the 2 MiB limit")
        key = self._derive(tenant_id, self.key_id)
        nonce = os.urandom(12)
        aad = self._aad(tenant_id, run_id, version, self.key_id)
        return EncryptedCheckpoint(
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
        version: int,
        ciphertext: bytes,
        nonce: bytes,
        fingerprint: str,
        key_id: str,
    ) -> dict:
        if key_id not in self._keys:
            raise IntegrityError("agent checkpoint key version is unavailable")
        key = self._derive(tenant_id, key_id)
        try:
            plaintext = AESGCM(key).decrypt(
                nonce,
                ciphertext,
                self._aad(tenant_id, run_id, version, key_id),
            )
        except InvalidTag as exc:
            raise IntegrityError("agent checkpoint authentication failed") from exc
        expected = hmac.new(key, plaintext, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, fingerprint):
            raise IntegrityError("agent checkpoint fingerprint is invalid")
        try:
            value = json.loads(plaintext)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IntegrityError("agent checkpoint plaintext is invalid") from exc
        if not isinstance(value, dict):
            raise IntegrityError("agent checkpoint must be an object")
        return value

    def request_digest(self, tenant_id: str, checkpoint: dict) -> str:
        return hmac.new(
            self._derive(tenant_id, self.key_id),
            self._canonical(checkpoint),
            hashlib.sha256,
        ).hexdigest()

    def request_digests(self, tenant_id: str, checkpoint: dict) -> frozenset[str]:
        canonical = self._canonical(checkpoint)
        return frozenset(
            hmac.new(self._derive(tenant_id, key_id), canonical, hashlib.sha256).hexdigest()
            for key_id in self._keys
        )

    @property
    def available_key_ids(self) -> frozenset[str]:
        return frozenset(self._keys)

    def _derive(self, tenant_id: str, key_id: str) -> bytes:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=hashlib.sha256(f"coifesp-agent-checkpoint:{key_id}".encode()).digest(),
            info=f"tenant:{tenant_id}:agent-checkpoint:v1".encode(),
        ).derive(self._keys[key_id])

    @staticmethod
    def _aad(tenant_id: str, run_id: str, version: int, key_id: str) -> bytes:
        return AgentCheckpointKeyring._canonical(
            {
                "schema": "coifesp.agent-checkpoint.ciphertext.v1",
                "tenant_id": tenant_id,
                "run_id": run_id,
                "version": version,
                "key_id": key_id,
            }
        )

    @staticmethod
    def _canonical(value: dict) -> bytes:
        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("agent checkpoint must be canonical JSON") from exc
