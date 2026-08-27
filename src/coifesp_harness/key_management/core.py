"""Provider-neutral KMS/Vault material boundary and resumable rotation workflow.

The provider protocol intentionally returns mutable material.  It permits local
zeroisation on cache expiry/close and prevents a configuration/environment
fallback from silently turning a production deployment into plaintext secrets.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import re
from threading import RLock
from time import monotonic
from typing import Callable, Protocol
from urllib.parse import urlparse

from ..agent_runs.crypto import AgentCheckpointKeyring
from ..agent_runs.control_crypto import AgentControlKeyring
from ..context.checkpoints import SemanticCheckpointKeyring
from ..memory.crypto import TenantMemoryKeyring
from ..postgres_audit import AuditSigningKeyring
from ..collaboration.gateway import SignedEnvelopeCodec
from ..tool_jobs.crypto import ToolJobKeyring


class KeyManagementError(RuntimeError):
    """A safe error that never includes key material or provider responses."""


class KeyPurpose(str, Enum):
    AUDIT = "audit"
    ENVELOPE = "envelope"
    MEMORY = "memory"
    AGENT_CHECKPOINT = "agent-checkpoint"
    AGENT_CONTROL = "agent-control"
    TOOL_JOB = "tool-job"
    SEMANTIC_CHECKPOINT = "semantic-checkpoint"


_PART = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
_TENANT = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")


@dataclass(frozen=True, slots=True)
class KeyReference:
    """A version-pinned URI: ``kms://tenant/purpose/name/versions/version``."""

    scheme: str
    tenant_domain: str
    purpose: KeyPurpose
    name: str
    version: str

    @classmethod
    def parse(cls, uri: str) -> "KeyReference":
        try:
            parsed = urlparse(uri)
            port = parsed.port
        except ValueError as exc:
            raise KeyManagementError("key reference is malformed") from exc
        if parsed.scheme not in {"kms", "vault"} or not parsed.netloc:
            raise KeyManagementError("key reference must use a kms or vault URI")
        if parsed.query or parsed.fragment or parsed.username or parsed.password or port:
            raise KeyManagementError("key reference must not include credentials or parameters")
        tenant = parsed.hostname or ""
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 4 or parts[2] != "versions" or parsed.path != "/" + "/".join(parts):
            raise KeyManagementError("key reference must pin an explicit version")
        try:
            purpose = KeyPurpose(parts[0])
        except ValueError as exc:
            raise KeyManagementError("key reference purpose is unsupported") from exc
        if not _TENANT.fullmatch(tenant) or any(not _PART.fullmatch(part) for part in (parts[1], parts[3])):
            raise KeyManagementError("key reference contains an invalid identifier")
        return cls(parsed.scheme, tenant, purpose, parts[1], parts[3])

    @property
    def uri(self) -> str:
        return f"{self.scheme}://{self.tenant_domain}/{self.purpose.value}/{self.name}/versions/{self.version}"

    def assert_binding(self, *, tenant_domain: str, purpose: KeyPurpose) -> None:
        if self.tenant_domain != tenant_domain or self.purpose is not purpose:
            raise KeyManagementError("key reference tenant or purpose binding is invalid")

    def __repr__(self) -> str:
        return f"KeyReference({self.scheme}://{self.tenant_domain}/{self.purpose.value}/…/versions/{self.version})"


class KeyProvider(Protocol):
    """Implementations call their KMS/Vault SDK; they must never log material."""

    def fetch(self, reference: KeyReference, *, tenant_domain: str, purpose: KeyPurpose) -> bytearray: ...

    def retire(self, reference: KeyReference, *, tenant_domain: str, purpose: KeyPurpose) -> None: ...


class FakeKeyProvider:
    """Deterministic test provider; never use it to source a production secret."""

    def __init__(self) -> None:
        self._keys: dict[str, bytes] = {}
        self.retired: list[str] = []
        self.fail_fetch = False
        self.fail_retire = False

    def install(self, reference: KeyReference, material: bytes) -> None:
        if len(material) != 32:
            raise ValueError("test key material must be exactly 32 bytes")
        self._keys[reference.uri] = bytes(material)

    def fetch(self, reference: KeyReference, *, tenant_domain: str, purpose: KeyPurpose) -> bytearray:
        reference.assert_binding(tenant_domain=tenant_domain, purpose=purpose)
        if self.fail_fetch or reference.uri not in self._keys:
            raise KeyManagementError("key provider could not resolve the requested version")
        return bytearray(self._keys[reference.uri])

    def retire(self, reference: KeyReference, *, tenant_domain: str, purpose: KeyPurpose) -> None:
        reference.assert_binding(tenant_domain=tenant_domain, purpose=purpose)
        if self.fail_retire:
            raise KeyManagementError("key provider could not retire the requested version")
        if reference.uri not in self.retired:
            self.retired.append(reference.uri)


@dataclass(slots=True)
class _CachedKey:
    material: bytearray = field(repr=False)
    expires_at: float = field(repr=False)


class KeyCache:
    """Small, explicit-lifetime cache that zeros its mutable copy on close."""

    def __init__(self, provider: KeyProvider, *, ttl_seconds: float = 30, clock: Callable[[], float] = monotonic) -> None:
        if not 0 < ttl_seconds <= 300:
            raise ValueError("key cache TTL must be between 0 and 300 seconds")
        self._provider, self._ttl, self._clock = provider, ttl_seconds, clock
        self._values: dict[str, _CachedKey] = {}
        self._closed = False
        self._lock = RLock()

    def resolve(self, reference: KeyReference, *, tenant_domain: str, purpose: KeyPurpose) -> bytearray:
        with self._lock:
            if self._closed:
                raise KeyManagementError("key cache is closed")
            reference.assert_binding(tenant_domain=tenant_domain, purpose=purpose)
            now = self._clock()
            self._purge_expired(now)
            cached = self._values.get(reference.uri)
            if cached is None:
                material = self._provider.fetch(reference, tenant_domain=tenant_domain, purpose=purpose)
                if not isinstance(material, bytearray) or len(material) != 32:
                    self._zero(material if isinstance(material, bytearray) else bytearray())
                    raise KeyManagementError("key provider returned invalid material")
                cached = _CachedKey(material, now + self._ttl)
                self._values[reference.uri] = cached
            return bytearray(cached.material)

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                for cached in self._values.values():
                    self._zero(cached.material)
                self._values.clear()
                self._closed = True

    def retire(self, reference: KeyReference, *, tenant_domain: str, purpose: KeyPurpose) -> None:
        """Retire only a validated, pinned reference through the configured provider."""
        with self._lock:
            if self._closed:
                raise KeyManagementError("key cache is closed")
            reference.assert_binding(tenant_domain=tenant_domain, purpose=purpose)
            self._provider.retire(reference, tenant_domain=tenant_domain, purpose=purpose)
            cached = self._values.pop(reference.uri, None)
            if cached is not None:
                self._zero(cached.material)

    def _purge_expired(self, now: float) -> None:
        expired = [uri for uri, cached in self._values.items() if cached.expires_at <= now]
        for uri in expired:
            self._zero(self._values.pop(uri).material)

    def __enter__(self) -> "KeyCache": return self
    def __exit__(self, *_: object) -> None: self.close()

    @staticmethod
    def _zero(material: bytearray) -> None:
        material[:] = b"\0" * len(material)


class ExistingKeyringAdapter:
    """Controlled bridge to legacy in-process keyrings, scoped by purpose."""

    def __init__(self, cache: KeyCache, *, tenant_domain: str) -> None:
        if not _TENANT.fullmatch(tenant_domain):
            raise ValueError("tenant_domain is invalid")
        self._cache, self._tenant = cache, tenant_domain

    def build(self, active: KeyReference, readable: tuple[KeyReference, ...] = ()) -> object:
        active.assert_binding(tenant_domain=self._tenant, purpose=active.purpose)
        refs = (active,) + tuple(reference for reference in readable if reference != active)
        if any(reference.purpose is not active.purpose for reference in refs):
            raise KeyManagementError("all dual-read references must share a purpose")
        copies = [self._cache.resolve(reference, tenant_domain=self._tenant, purpose=active.purpose) for reference in refs]
        try:
            keys = {reference.version: bytes(material) for reference, material in zip(refs, copies, strict=True)}
            if active.purpose is KeyPurpose.AUDIT:
                return AuditSigningKeyring(active_key_id=active.version, verification_keys=keys)
            if active.purpose is KeyPurpose.ENVELOPE:
                return SignedEnvelopeCodec(active_key_id=active.version, verification_keys=keys)
            if active.purpose is KeyPurpose.MEMORY:
                return TenantMemoryKeyring(active_key_id=active.version, decryption_keys=keys)
            if active.purpose is KeyPurpose.AGENT_CHECKPOINT:
                return AgentCheckpointKeyring(active_key_id=active.version, decryption_keys=keys)
            if active.purpose is KeyPurpose.AGENT_CONTROL:
                return AgentControlKeyring(active_key_id=active.version, decryption_keys=keys)
            if active.purpose is KeyPurpose.TOOL_JOB:
                return ToolJobKeyring(active_key_id=active.version, decryption_keys=keys)
            if active.purpose is KeyPurpose.SEMANTIC_CHECKPOINT:
                return SemanticCheckpointKeyring(active_key_id=active.version, keys=keys)
            raise KeyManagementError("key purpose has no compatibility adapter")
        finally:
            for material in copies:
                KeyCache._zero(material)


class RotationStage(str, Enum):
    PREPARE = "prepare"
    DUAL_READ = "dual-read"
    NEW_WRITE = "new-write"
    REENCRYPT = "reencrypt"
    RETIRE = "retire"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class Actor:
    actor_id: str
    roles: frozenset[str]


@dataclass(frozen=True, slots=True)
class ReencryptResult:
    checkpoint: str | None
    complete: bool


@dataclass(frozen=True, slots=True)
class RotationPlan:
    plan_id: str
    tenant_domain: str
    purpose: KeyPurpose
    old: KeyReference
    new: KeyReference
    stage: RotationStage = RotationStage.PREPARE
    checkpoint: str | None = None
    prepared_by: str | None = None
    reencrypted_by: str | None = None
    history: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _PART.fullmatch(self.plan_id) or not _TENANT.fullmatch(self.tenant_domain) or self.old == self.new:
            raise ValueError("rotation plan is invalid")
        self.old.assert_binding(tenant_domain=self.tenant_domain, purpose=self.purpose)
        self.new.assert_binding(tenant_domain=self.tenant_domain, purpose=self.purpose)

    def __repr__(self) -> str:
        return f"RotationPlan(plan_id={self.plan_id!r}, stage={self.stage.value!r}, purpose={self.purpose.value!r})"


class RotationStore(Protocol):
    def get(self, plan_id: str) -> RotationPlan: ...
    def save(self, plan: RotationPlan) -> None: ...


class _MemoryRotationStore:
    def __init__(self) -> None: self.values: dict[str, RotationPlan] = {}
    def get(self, plan_id: str) -> RotationPlan:
        try: return self.values[plan_id]
        except KeyError as exc: raise KeyManagementError("rotation plan does not exist") from exc
    def save(self, plan: RotationPlan) -> None: self.values[plan.plan_id] = plan


class RotationOrchestrator:
    """Idempotent workflow; callers persist this store transactionally in production."""

    def __init__(self, cache: KeyCache, *, store: RotationStore | None = None) -> None:
        self._cache, self._store = cache, store or _MemoryRotationStore()

    def create(self, plan: RotationPlan) -> RotationPlan:
        try:
            existing = self._store.get(plan.plan_id)
        except KeyManagementError:
            self._store.save(plan)
            return plan
        if existing.old != plan.old or existing.new != plan.new or existing.purpose is not plan.purpose:
            raise KeyManagementError("rotation plan identifier conflicts with an existing plan")
        return existing

    def prepare(self, plan_id: str, actor: Actor) -> RotationPlan:
        plan = self._store.get(plan_id)
        if plan.stage is not RotationStage.PREPARE:
            return plan
        self._require(actor, "key-custodian")
        for ref in (plan.old, plan.new):
            material = self._cache.resolve(ref, tenant_domain=plan.tenant_domain, purpose=plan.purpose)
            KeyCache._zero(material)
        return self._save(plan, RotationStage.DUAL_READ, actor, prepared_by=actor.actor_id)

    def enable_new_writes(self, plan_id: str, actor: Actor) -> RotationPlan:
        plan = self._store.get(plan_id)
        if plan.stage in {RotationStage.NEW_WRITE, RotationStage.REENCRYPT, RotationStage.RETIRE, RotationStage.COMPLETE}:
            return plan
        if plan.stage is not RotationStage.DUAL_READ:
            raise KeyManagementError("rotation plan is not ready for new writes")
        self._require(actor, "rotation-operator")
        if actor.actor_id == plan.prepared_by:
            raise KeyManagementError("rotation operator must differ from key custodian")
        return self._save(plan, RotationStage.NEW_WRITE, actor)

    def begin_reencryption(self, plan_id: str, actor: Actor) -> RotationPlan:
        plan = self._store.get(plan_id)
        if plan.stage is RotationStage.REENCRYPT:
            return plan
        if plan.stage is not RotationStage.NEW_WRITE:
            raise KeyManagementError("rotation plan is not ready for reencryption")
        self._require(actor, "rotation-operator")
        if actor.actor_id == plan.prepared_by:
            raise KeyManagementError("rotation operator must differ from key custodian")
        return self._save(plan, RotationStage.REENCRYPT, actor)

    def reencrypt_batch(self, plan_id: str, actor: Actor, work: Callable[[RotationPlan], ReencryptResult]) -> RotationPlan:
        plan = self._store.get(plan_id)
        if plan.stage in {RotationStage.RETIRE, RotationStage.COMPLETE}:
            return plan
        if plan.stage is not RotationStage.REENCRYPT:
            raise KeyManagementError("rotation plan is not in reencryption")
        self._require(actor, "rotation-operator")
        result = work(plan)  # failure leaves the prior checkpoint intact for recovery
        if not isinstance(result, ReencryptResult) or result.complete and result.checkpoint is not None:
            raise KeyManagementError("reencryption worker returned an invalid checkpoint")
        stage = RotationStage.RETIRE if result.complete else RotationStage.REENCRYPT
        updated = replace(plan, stage=stage, checkpoint=result.checkpoint, reencrypted_by=actor.actor_id,
                          history=plan.history + (f"{stage.value}:{actor.actor_id}",))
        self._store.save(updated)
        return updated

    def retire(self, plan_id: str, actor: Actor, completion_gate: Callable[[RotationPlan], bool]) -> RotationPlan:
        plan = self._store.get(plan_id)
        if plan.stage is RotationStage.COMPLETE:
            return plan
        if plan.stage is not RotationStage.RETIRE:
            raise KeyManagementError("rotation completion gate has not been reached")
        self._require(actor, "key-custodian")
        if actor.actor_id in {plan.prepared_by, plan.reencrypted_by}:
            raise KeyManagementError("retirement must be approved by an independent key custodian")
        if not completion_gate(plan):
            raise KeyManagementError("rotation completion gate rejected retirement")
        self._cache.retire(plan.old, tenant_domain=plan.tenant_domain, purpose=plan.purpose)
        return self._save(plan, RotationStage.COMPLETE, actor)

    def _save(self, plan: RotationPlan, stage: RotationStage, actor: Actor, **changes: object) -> RotationPlan:
        updated = replace(plan, stage=stage, history=plan.history + (f"{stage.value}:{actor.actor_id}",), **changes)
        self._store.save(updated)
        return updated

    @staticmethod
    def _require(actor: Actor, role: str) -> None:
        if not actor.actor_id or role not in actor.roles:
            raise KeyManagementError("actor is not authorised for this rotation action")
