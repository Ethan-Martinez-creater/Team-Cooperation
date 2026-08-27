"""Validate the dedicated Tool Worker service identity without printing secrets."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import jwt
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coifesp_harness.auth import (  # noqa: E402
    ClientCredentialsConfig,
    ClientCredentialsTokenProvider,
    OIDCVerifier,
    OIDCWorkerIdentityProvider,
)
from coifesp_harness.config import Environment, Settings  # noqa: E402


async def check(settings: Settings) -> None:
    settings.validate(require_auth=True, require_memory=True, require_tool_worker=True)
    assert settings.tool_worker_token_endpoint is not None
    assert settings.tool_worker_client_id is not None
    assert settings.tool_worker_client_secret is not None
    assert settings.tool_worker_tenant_id is not None
    tokens = ClientCredentialsTokenProvider(
        ClientCredentialsConfig(
            token_endpoint=settings.tool_worker_token_endpoint,
            client_id=settings.tool_worker_client_id,
            client_secret=settings.tool_worker_client_secret,
            allow_insecure_http=settings.environment is not Environment.PRODUCTION,
        )
    )
    verifier = OIDCVerifier(settings=settings)
    try:
        raw = await tokens.token()
        claims = jwt.decode(
            raw.reveal(),
            options={
                "verify_signature": False,
                "verify_exp": False,
                "verify_aud": False,
            },
        )
        audience = claims.get("aud")
        audiences = {audience} if isinstance(audience, str) else set(audience or [])
        roles = claims.get(settings.oidc_roles_claim)
        role_values = set(roles) if isinstance(roles, list) else set()
        checks = {
            "issuer": claims.get("iss") == settings.oidc_issuer,
            "audience": settings.oidc_audience in audiences,
            "authorized_party": claims.get("azp") in settings.oidc_authorized_parties,
            "tenant": claims.get(settings.oidc_tenant_claim) == settings.tool_worker_tenant_id,
            "role": "tool_worker" in role_values,
            "service": claims.get("token_use") == "service",
            "compartments": isinstance(claims.get(settings.oidc_compartments_claim), list),
            "clearance": isinstance(claims.get(settings.oidc_clearance_claim), str),
        }
        if not all(checks.values()):
            failed = ",".join(name for name, passed in checks.items() if not passed)
            raise RuntimeError(f"token claim checks failed: {failed}")
        identity = await OIDCWorkerIdentityProvider(
            tokens=tokens,
            verifier=verifier,
            required_role="tool_worker",
            expected_tenant_id=settings.tool_worker_tenant_id,
        ).resolve()
        if "agent_worker" in identity.roles:
            raise RuntimeError("Tool Worker service account also has agent_worker role")
        if settings.worker_client_id == settings.tool_worker_client_id:
            raise RuntimeError("Tool Worker and Agent Worker clients are not distinct")
        print(
            "TOOL_WORKER_OIDC_OK issuer=keycloak service=yes "
            f"tenant={identity.tenant_id} role=tool_worker least_privilege=yes "
            "distinct_agent_worker=yes secrets=redacted"
        )
    finally:
        await tokens.aclose()
        await verifier.aclose()


def main() -> int:
    try:
        load_dotenv(ROOT / ".env", override=True)
        asyncio.run(check(Settings.from_environment()))
        return 0
    except Exception as exc:
        print(
            f"TOOL_WORKER_OIDC_FAILED error_type={type(exc).__name__} "
            f"reason={exc} secrets=redacted"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
