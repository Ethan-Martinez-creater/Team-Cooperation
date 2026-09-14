import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from coifesp_harness.config import SecretValue
from coifesp_harness.connectors import (
    ConnectorEndpoint,
    ReviewedConnectorCatalog,
    SQLAlchemyConnectorRegistry,
)
from coifesp_harness.connectors.repository import CONNECTOR_REGISTRATIONS
from coifesp_harness.errors import GovernanceConflictError, ResourceNotFound
from coifesp_harness.postgres_audit import AuditSigningKeyring, SQLAlchemyAuditLog
from coifesp_harness.security import Classification, Principal


def endpoint(base_url="https://api.office.test"):
    return ConnectorEndpoint(connector_id="office-main", tenant_id="team-a",
        base_url=base_url, token_endpoint="https://id.office.test/token",
        client_id="client", client_secret=SecretValue("x"*32),
        scopes=("message.send",), allowed_paths=frozenset({"/v1/messages"}),
        max_classification=Classification.INTERNAL)


def stack():
    engine=create_engine("sqlite+pysqlite:///:memory:",connect_args={"check_same_thread":False},poolclass=StaticPool)
    audit=SQLAlchemyAuditLog(engine=engine,keyring=AuditSigningKeyring(active_key_id="a",verification_keys={"a":b"a"*32}))
    audit.create_schema(); registry=SQLAlchemyConnectorRegistry(engine=engine,audit_log=audit); registry.create_schema()
    admin=Principal("admin","team-a",roles=frozenset({"connector_administrator"}))
    reviewer=Principal("reviewer","team-a",roles=frozenset({"connector_reviewer"}))
    return engine,audit,registry,admin,reviewer


def test_connector_revisions_require_independent_review_and_preserve_active_config():
    engine,audit,registry,admin,reviewer=stack()
    assert registry.propose(principal=admin,endpoint=endpoint(),client_secret_env="COIFESP_CONNECTOR_OFFICE_CLIENT_SECRET")==("office-main","pending",1)
    with pytest.raises(GovernanceConflictError,match="separation"):
        registry.review(principal=Principal("admin","team-a",roles=frozenset({"connector_reviewer"})),connector_id="office-main",expected_version=1,approve=True,reason="self")
    registry.review(principal=reviewer,connector_id="office-main",expected_version=1,approve=True,reason="scope reviewed")
    env={"COIFESP_CONNECTOR_OFFICE_CLIENT_SECRET":"s"*32}
    assert registry.active_endpoint(principal=admin,connector_id="office-main",environment=env).base_url=="https://api.office.test"
    registry.propose(principal=admin,endpoint=endpoint("https://api-v2.office.test"),client_secret_env="COIFESP_CONNECTOR_OFFICE_CLIENT_SECRET")
    assert registry.active_endpoint(principal=admin,connector_id="office-main",environment=env).base_url=="https://api.office.test"
    registry.review(principal=reviewer,connector_id="office-main",expected_version=2,approve=False,reason="origin not approved")
    assert registry.active_endpoint(principal=admin,connector_id="office-main",environment=env).base_url=="https://api.office.test"
    registry.request_disable(principal=admin,connector_id="office-main")
    assert registry.active_endpoint(principal=admin,connector_id="office-main",environment=env)
    registry.review(principal=reviewer,connector_id="office-main",expected_version=3,approve=True,reason="retired")
    with pytest.raises(ResourceNotFound): registry.active_endpoint(principal=admin,connector_id="office-main",environment=env)
    with engine.connect() as c:
        rows=c.execute(select(CONNECTOR_REGISTRATIONS)).mappings().all()
    assert all("s"*32 not in str(row) for row in rows)
    assert audit.verify_tenant_chain("team-a")==6


def test_worker_catalog_requires_active_review_and_deployment_allowlist():
    engine, _audit, registry, admin, reviewer = stack()
    environment = {"COIFESP_CONNECTOR_OFFICE_CLIENT_SECRET": "s" * 32}
    catalog = ReviewedConnectorCatalog(
        registry=registry,
        tenant_id="team-a",
        allowed_connector_ids=frozenset({"office-main"}),
        environment=environment,
    )

    registry.propose(
        principal=admin,
        endpoint=endpoint(),
        client_secret_env="COIFESP_CONNECTOR_OFFICE_CLIENT_SECRET",
    )
    assert catalog.get(tenant_id="team-a", connector_id="office-main") is None
    registry.review(
        principal=reviewer,
        connector_id="office-main",
        expected_version=1,
        approve=True,
        reason="approved for worker execution",
    )
    assert catalog.get(tenant_id="team-a", connector_id="office-main").base_url == (
        "https://api.office.test"
    )
    assert catalog.get(tenant_id="team-b", connector_id="office-main") is None
    assert catalog.get(tenant_id="team-a", connector_id="not-allowed") is None

    registry.request_disable(principal=admin, connector_id="office-main")
    registry.review(
        principal=reviewer,
        connector_id="office-main",
        expected_version=2,
        approve=True,
        reason="connector retired",
    )
    assert catalog.get(tenant_id="team-a", connector_id="office-main") is None
    engine.dispose()


def test_worker_catalog_enforces_exact_tenant_connector_pairs():
    class Registry:
        def active_endpoint_for_worker(self, *, tenant_id, connector_id, environment):
            return (tenant_id, connector_id, environment)

    catalog = ReviewedConnectorCatalog(
        registry=Registry(),
        allowed_tenant_ids=frozenset({"team-a", "team-b"}),
        allowed_pairs=frozenset(
            {("team-a", "connector-x"), ("team-b", "connector-y")}
        ),
        environment={"DEPLOYMENT": "test"},
    )

    assert catalog.get(tenant_id="team-a", connector_id="connector-x") is not None
    assert catalog.get(tenant_id="team-b", connector_id="connector-y") is not None
    assert catalog.get(tenant_id="team-b", connector_id="connector-x") is None
    assert catalog.get(tenant_id="team-a", connector_id="connector-y") is None
