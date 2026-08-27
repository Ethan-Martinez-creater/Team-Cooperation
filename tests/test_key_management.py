import pytest

from coifesp_harness.key_management import (
    Actor, ExistingKeyringAdapter, FakeKeyProvider, KeyCache, KeyManagementError,
    KeyPurpose, KeyReference, ReencryptResult, RotationOrchestrator, RotationPlan,
    RotationStage,
)


TENANT = "acme.example"


def ref(purpose, version):
    return KeyReference.parse(f"kms://{TENANT}/{purpose.value}/root/versions/{version}")


def test_uri_binding_and_cache_expiry_zeroisation_are_strict():
    provider = FakeKeyProvider(); key = ref(KeyPurpose.MEMORY, "v1"); provider.install(key, b"a" * 32)
    now = [0.0]; cache = KeyCache(provider, ttl_seconds=1, clock=lambda: now[0])
    copy = cache.resolve(key, tenant_domain=TENANT, purpose=KeyPurpose.MEMORY)
    assert copy == b"a" * 32
    now[0] = 2; cache.resolve(key, tenant_domain=TENANT, purpose=KeyPurpose.MEMORY)
    assert cache._values[key.uri].material == b"a" * 32
    cache.close()
    assert cache._closed and not cache._values
    with pytest.raises(KeyManagementError): cache.resolve(key, tenant_domain=TENANT, purpose=KeyPurpose.MEMORY)
    with pytest.raises(KeyManagementError): KeyReference.parse("kms://acme.example/memory/root")
    with pytest.raises(KeyManagementError): key.assert_binding(tenant_domain="other.example", purpose=KeyPurpose.MEMORY)


def test_adapter_dual_reads_without_material_in_repr():
    provider = FakeKeyProvider(); old, new = ref(KeyPurpose.MEMORY, "v1"), ref(KeyPurpose.MEMORY, "v2")
    provider.install(old, b"o" * 32); provider.install(new, b"n" * 32)
    with KeyCache(provider) as cache:
        ring = ExistingKeyringAdapter(cache, tenant_domain=TENANT).build(new, (old,))
        assert ring.key_id == "v2" and ring.available_key_ids == frozenset({"v1", "v2"})
        assert b"n" * 32 not in repr(ring).encode()
    with pytest.raises(KeyManagementError):
        ExistingKeyringAdapter(KeyCache(provider), tenant_domain=TENANT).build(new, (ref(KeyPurpose.TOOL_JOB, "v1"),))


@pytest.mark.parametrize("purpose", [KeyPurpose.AUDIT, KeyPurpose.ENVELOPE])
def test_adapter_covers_signing_keyrings(purpose):
    provider = FakeKeyProvider(); old, new = ref(purpose, "v1"), ref(purpose, "v2")
    provider.install(old, b"o" * 32); provider.install(new, b"n" * 32)
    with KeyCache(provider) as cache:
        ring = ExistingKeyringAdapter(cache, tenant_domain=TENANT).build(new, (old,))
        assert ring.active_key_id == "v2"


def test_retirement_evicts_cached_old_material_and_is_provider_idempotent():
    provider = FakeKeyProvider(); old = ref(KeyPurpose.MEMORY, "v1")
    provider.install(old, b"o" * 32)
    cache = KeyCache(provider)
    cache.resolve(old, tenant_domain=TENANT, purpose=KeyPurpose.MEMORY)
    cached_material = cache._values[old.uri].material
    cache.retire(old, tenant_domain=TENANT, purpose=KeyPurpose.MEMORY)
    cache.retire(old, tenant_domain=TENANT, purpose=KeyPurpose.MEMORY)
    assert cached_material == b"\0" * 32 and old.uri not in cache._values
    assert provider.retired == [old.uri]


def test_rotation_is_resumable_separated_and_never_retires_on_worker_failure():
    provider = FakeKeyProvider(); old, new = ref(KeyPurpose.MEMORY, "v1"), ref(KeyPurpose.MEMORY, "v2")
    provider.install(old, b"o" * 32); provider.install(new, b"n" * 32)
    orchestrator = RotationOrchestrator(KeyCache(provider))
    plan = orchestrator.create(RotationPlan("rotate-1", TENANT, KeyPurpose.MEMORY, old, new))
    custodian = Actor("custodian-a", frozenset({"key-custodian"}))
    operator = Actor("operator-b", frozenset({"rotation-operator"}))
    approver = Actor("custodian-c", frozenset({"key-custodian"}))
    assert orchestrator.prepare(plan.plan_id, custodian).stage is RotationStage.DUAL_READ
    assert orchestrator.enable_new_writes(plan.plan_id, operator).stage is RotationStage.NEW_WRITE
    orchestrator.begin_reencryption(plan.plan_id, operator)
    with pytest.raises(RuntimeError): orchestrator.reencrypt_batch(plan.plan_id, operator, lambda _: (_ for _ in ()).throw(RuntimeError("provider data failure")))
    assert orchestrator._store.get(plan.plan_id).checkpoint is None
    assert orchestrator.reencrypt_batch(plan.plan_id, operator, lambda _: ReencryptResult("page-2", False)).checkpoint == "page-2"
    plan = orchestrator.reencrypt_batch(plan.plan_id, operator, lambda _: ReencryptResult(None, True))
    assert plan.stage is RotationStage.RETIRE
    with pytest.raises(KeyManagementError): orchestrator.retire(plan.plan_id, custodian, lambda _: True)
    with pytest.raises(KeyManagementError): orchestrator.retire(plan.plan_id, approver, lambda _: False)
    assert orchestrator.retire(plan.plan_id, approver, lambda _: True).stage is RotationStage.COMPLETE
    assert provider.retired == [old.uri]
