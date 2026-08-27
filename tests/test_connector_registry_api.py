import asyncio
from datetime import UTC, datetime, timedelta
import httpx
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from coifesp_harness.auth import VerifiedIdentity
from coifesp_harness.config import Settings
from coifesp_harness.connectors import SQLAlchemyConnectorRegistry
from coifesp_harness.control_plane import create_app
from coifesp_harness.errors import AuthenticationError
from coifesp_harness.postgres_audit import AuditSigningKeyring, SQLAlchemyAuditLog
from coifesp_harness.security import Classification, Principal

class Verifier:
    def __init__(self,values):self.values=values
    async def verify(self,token):
        if token not in self.values:raise AuthenticationError()
        return self.values[token]
def identity(pid,roles):return VerifiedIdentity(Principal(pid,"team-a",roles=frozenset(roles),clearance=Classification.RESTRICTED),"https://id.test","control",datetime.now(UTC)+timedelta(minutes=5),None)
async def call(app,method,path,token,**kwargs):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,raise_app_exceptions=False),base_url="https://control.test") as c:
        return await c.request(method,path,headers={"Authorization":f"Bearer {token}"},**kwargs)

def test_connector_registry_api_exposes_review_projection_not_secret_material():
    engine=create_engine("sqlite+pysqlite:///:memory:",connect_args={"check_same_thread":False},poolclass=StaticPool)
    audit=SQLAlchemyAuditLog(engine=engine,keyring=AuditSigningKeyring(active_key_id="a",verification_keys={"a":b"a"*32}));audit.create_schema()
    registry=SQLAlchemyConnectorRegistry(engine=engine,audit_log=audit);registry.create_schema()
    settings=Settings.from_environment({"COIFESP_ENV":"test","COIFESP_OIDC_ISSUER":"https://id.test","COIFESP_OIDC_AUDIENCE":"control","COIFESP_OIDC_AUTHORIZED_PARTIES":"client","COIFESP_OIDC_JWKS_URL":"https://id.test/jwks"})
    app=create_app(settings=settings,verifier=Verifier({"admin":identity("admin",{"connector_administrator"}),"reviewer":identity("reviewer",{"connector_reviewer"})}),connector_registry=registry)
    body={"connector_id":"office-main","base_url":"https://api.office.test","token_endpoint":"https://id.office.test/token","client_id":"client","client_secret_env":"COIFESP_CONNECTOR_OFFICE_CLIENT_SECRET","scopes":["message.send"],"allowed_paths":["/v1/messages"],"max_classification":"internal","timeout_seconds":15,"max_response_bytes":1048576,"max_attempts":3,"circuit_failure_threshold":5,"circuit_cooldown_seconds":30}
    created=asyncio.run(call(app,"POST","/v1/connectors","admin",json=body))
    projection=asyncio.run(call(app,"GET","/v1/connectors/office-main/revisions/1","reviewer"))
    self_review=asyncio.run(call(app,"POST","/v1/connectors/office-main/revisions/1:review","admin",json={"approve":True,"reason":"self"}))
    approved=asyncio.run(call(app,"POST","/v1/connectors/office-main/revisions/1:review","reviewer",json={"approve":True,"reason":"origin and scopes reviewed"}))
    assert created.status_code==202 and created.json()["status"]=="pending"
    assert projection.status_code==200 and projection.json()["client_secret_env"]=="COIFESP_CONNECTOR_OFFICE_CLIENT_SECRET"
    assert set(key for key in projection.json() if "secret" in key)=={"client_secret_env"}
    assert "xxxxxxxx" not in projection.text
    assert self_review.status_code in {403,409}
    assert approved.status_code==200 and approved.json()["status"]=="active"
