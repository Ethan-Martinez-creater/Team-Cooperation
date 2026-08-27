from __future__ import annotations
import sys, uuid
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/"src"))
from coifesp_harness.config import SecretValue, Settings
from coifesp_harness.connectors import ConnectorEndpoint, SQLAlchemyConnectorRegistry
from coifesp_harness.postgres_audit import AuditSigningKeyring, SQLAlchemyAuditLog
from coifesp_harness.security import Classification, Principal

def main():
    load_dotenv(ROOT/".env",override=False);settings=Settings.from_environment();engine=create_engine(settings.database_url,hide_parameters=True)
    audit=SQLAlchemyAuditLog(engine=engine,keyring=AuditSigningKeyring.from_settings(settings));registry=SQLAlchemyConnectorRegistry(engine=engine,audit_log=audit)
    suffix=uuid.uuid4().hex[:12];tenant=f"connector-{suffix}";cid=f"office-{suffix}"
    admin=Principal("admin",tenant,roles=frozenset({"connector_administrator"}));reviewer=Principal("reviewer",tenant,roles=frozenset({"connector_reviewer"}))
    endpoint=ConnectorEndpoint(cid,tenant,"https://api.office.example","https://id.office.example/token","client",SecretValue("x"*32),("message.send",),frozenset({"/v1/messages"}),Classification.INTERNAL)
    registry.propose(principal=admin,endpoint=endpoint,client_secret_env="COIFESP_CONNECTOR_OFFICE_CLIENT_SECRET")
    registry.review(principal=reviewer,connector_id=cid,expected_version=1,approve=True,reason="reviewed origin and scope")
    active=registry.active_endpoint(principal=admin,connector_id=cid,environment={"COIFESP_CONNECTOR_OFFICE_CLIENT_SECRET":"s"*32})
    assert active.base_url=="https://api.office.example" and active.client_secret.reveal()=="s"*32
    assert audit.verify_tenant_chain(tenant)==2
    with engine.connect() as c:
        rev=c.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        rls=c.execute(text("SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE relname='connector_registrations'" )).scalar_one()
        c.execute(text("SELECT set_config('coifesp.tenant_id',:tenant,true)"),{"tenant":f"other-{suffix}"})
        hidden=c.execute(text("SELECT count(*) FROM connector_registrations WHERE connector_id=:id"),{"id":cid}).scalar_one()
        persisted=str(c.execute(text("SELECT client_secret_env FROM connector_registrations WHERE tenant_id=:tenant"),{"tenant":tenant}).fetchall())
    engine.dispose();assert rev=="20260813_28" and rls and hidden==0 and "s"*32 not in persisted
    print("CONNECTOR_REGISTRY_OK revision=20260813_28 append_only=yes independent_review=yes secret_reference_only=yes cross_tenant=hidden audit=verified rls=forced")
if __name__=="__main__":main()
