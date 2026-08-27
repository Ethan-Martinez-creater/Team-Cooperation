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
from ..errors import MemoryIntegrityError
from ..key_material import memory_keys_from_settings
from .models import MemoryKind, MemoryScope
from ..security.models import ResourceLabel


@dataclass(frozen=True, slots=True)
class EncryptedPayload:
    ciphertext: bytes
    nonce: bytes
    content_fingerprint: str
    key_id: str


class TenantMemoryKeyring:
    """Derives a different AES-256 key for every tenant using HKDF-SHA256."""

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
            raise ValueError("memory keyring is invalid")
        self._keys = {name: bytes(value) for name, value in keys.items()}
        self.key_id = active

    @classmethod
    def from_settings(cls, settings: Settings) -> "TenantMemoryKeyring":
        active, keys = memory_keys_from_settings(settings)
        return cls(active_key_id=active, decryption_keys=keys)

    def encrypt(
        self,
        *,
        memory_id: str,
        tenant_id: str,
        scope: MemoryScope,
        kind: MemoryKind,
        label: ResourceLabel,
        plaintext: str,
    ) -> EncryptedPayload:
        key = self._derive_tenant_key(tenant_id, self.key_id)
        aad = self._aad(
            memory_id=memory_id,
            tenant_id=tenant_id,
            scope=scope,
            kind=kind,
            label=label,
            key_id=self.key_id,
        )
        nonce = os.urandom(12)
        encoded = plaintext.encode("utf-8")
        ciphertext = AESGCM(key).encrypt(nonce, encoded, aad)
        fingerprint = hmac.new(key, encoded, hashlib.sha256).hexdigest()
        return EncryptedPayload(
            ciphertext=ciphertext,
            nonce=nonce,
            content_fingerprint=fingerprint,
            key_id=self.key_id,
        )

    def decrypt(self, record) -> str:
        if record.key_id not in self._keys:
            raise MemoryIntegrityError(f"memory key version is unavailable: {record.key_id}")
        key = self._derive_tenant_key(record.tenant_id, record.key_id)
        aad = self._aad(
            memory_id=record.memory_id,
            tenant_id=record.tenant_id,
            scope=record.scope,
            kind=record.kind,
            label=record.label,
            key_id=record.key_id,
        )
        try:
            plaintext = AESGCM(key).decrypt(record.nonce, record.ciphertext, aad)
        except InvalidTag as exc:
            raise MemoryIntegrityError(
                "memory ciphertext or authenticated metadata was modified"
            ) from exc
        expected = hmac.new(key, plaintext, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, record.content_fingerprint):
            raise MemoryIntegrityError("memory content fingerprint is invalid")
        try:
            return plaintext.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MemoryIntegrityError("memory plaintext is not UTF-8") from exc

    def request_fingerprint(self, tenant_id: str, canonical_request: bytes) -> str:
        return hmac.new(
            self._derive_tenant_key(tenant_id, self.key_id),
            canonical_request,
            hashlib.sha256,
        ).hexdigest()

    def request_fingerprints(self, tenant_id: str, canonical_request: bytes) -> frozenset[str]:
        return frozenset(
            hmac.new(
                self._derive_tenant_key(tenant_id, key_id),
                canonical_request,
                hashlib.sha256,
            ).hexdigest()
            for key_id in self._keys
        )

    def search_token(self, tenant_id: str, token: str, *, key_id: str | None = None) -> str:
        version = key_id or self.key_id
        if version not in self._keys:
            raise MemoryIntegrityError("memory search key version is unavailable")
        key = HKDF(
            algorithm=hashes.SHA256(), length=32,
            salt=hashlib.sha256(f"coifesp-memory-search:{version}".encode()).digest(),
            info=f"tenant:{tenant_id}".encode(),
        ).derive(self._keys[version])
        return hmac.new(key, token.encode("utf-8"), hashlib.sha256).hexdigest()

    def search_tokens(self, tenant_id: str, tokens: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
        return {
            key_id: tuple(self.search_token(tenant_id, token, key_id=key_id) for token in tokens)
            for key_id in self._keys
        }

    @property
    def available_key_ids(self) -> frozenset[str]:
        return frozenset(self._keys)

    def _derive_tenant_key(self, tenant_id: str, key_id: str) -> bytes:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        salt = hashlib.sha256(f"coifesp-memory:{key_id}".encode("utf-8")).digest()
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            info=f"tenant:{tenant_id}:memory:v1".encode("utf-8"),
        ).derive(self._keys[key_id])

    @staticmethod
    def _aad(
        *,
        memory_id: str,
        tenant_id: str,
        scope: MemoryScope,
        kind: MemoryKind,
        label: ResourceLabel,
        key_id: str,
    ) -> bytes:
        value = {
            "schema": "coifesp.memory.ciphertext.v1",
            "memory_id": memory_id,
            "tenant_id": tenant_id,
            "scope": scope.value,
            "kind": kind.value,
            "owner_tenant_id": label.owner_tenant_id,
            "classification": int(label.classification),
            "compartments": sorted(label.compartments),
            "resource_id": label.resource_id,
            "key_id": key_id,
        }
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
