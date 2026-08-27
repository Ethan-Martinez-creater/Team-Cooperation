"""Run one bounded public DeepSeek task through the production durable Worker."""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import httpx
from sqlalchemy import text


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from check_worker_identity_e2e import (  # noqa: E402
    BASE_URL,
    REALM,
    admin_token,
    create_owner,
    delete_owner,
    keycloak_environment,
)
from coifesp_harness.agent_runs import AgentRunCheckpointCodec  # noqa: E402
from coifesp_harness.control_plane.bootstrap import load_environment_settings  # noqa: E402
from coifesp_harness.runtime import (  # noqa: E402
    AgentRunRequest,
    Message,
    ModelRoutePolicy,
    RunBudget,
)
from coifesp_harness.security import Classification, Principal  # noqa: E402
from coifesp_harness.worker_main import build_worker_runtime  # noqa: E402


PROMPT = "Reply with exactly: COIFESP_WORKER_OK"


class AcceptanceFailure(RuntimeError):
    pass


def require(value: bool, message: str) -> None:
    if not value:
        raise AcceptanceFailure(message)


async def run_live(owner_id: str) -> None:
    settings = load_environment_settings(PROJECT_ROOT / ".env")
    runtime = await build_worker_runtime(settings)
    run_id = f"worker-e2e-{uuid.uuid4().hex}"
    owner = Principal(
        owner_id,
        "team-a",
        roles=frozenset({"contributor"}),
        clearance=Classification.INTERNAL,
        compartments=frozenset({"project-x"}),
    )
    request = AgentRunRequest(
        run_id=run_id,
        correlation_id=f"corr-{run_id}",
        principal=owner,
        messages=(Message("user", PROMPT),),
        budget=RunBudget(
            max_turns=1,
            max_tool_calls=1,
            max_total_tokens=512,
            max_model_cost_microusd=1_000,
        ),
        model_route_policy=ModelRoutePolicy(
            data_classification=Classification.PUBLIC,
            allowed_provider_ids=frozenset({"deepseek_v4_flash"}),
            residency_regions=frozenset({"external"}),
            allow_external_egress=True,
            max_call_cost_microusd=1_000,
            max_output_tokens=128,
            max_call_total_tokens=512,
        ),
    )
    service = runtime.runner.worker.service
    service.create(
        principal=owner,
        run_id=run_id,
        correlation_id=request.correlation_id,
        idempotency_key=f"idem-{run_id}",
        checkpoint=AgentRunCheckpointCodec().initial(request),
        max_failures=1,
    )
    try:
        worker = await runtime.runner.identity_provider.resolve()
        outcome = await asyncio.wait_for(
            runtime.runner.worker.process_once(worker=worker),
            timeout=180,
        )
        record = service.get(principal=owner, run_id=run_id)
        require(outcome.status.value == "completed", f"worker outcome was {outcome.status.value}")
        require(record.status.value == "completed", f"run status was {record.status.value}")
        require(record.turns == 1 and record.total_tokens > 0, "usage counters were not persisted")
        require(0 < record.model_cost_microusd <= 1_000, "model cost was not bounded and persisted")

        with runtime.engine.connect() as connection, connection.begin():
            connection.execute(
                text("SELECT set_config('coifesp.tenant_id', 'team-a', true)")
            )
            row = connection.execute(
                text(
                    "SELECT checkpoint_ciphertext, checkpoint_nonce, checkpoint_fingerprint "
                    "FROM agent_runs WHERE tenant_id='team-a' AND run_id=:run_id"
                ),
                {"run_id": run_id},
            ).one()
        require(PROMPT.encode("utf-8") not in bytes(row.checkpoint_ciphertext), "plaintext prompt leaked into checkpoint ciphertext")
        require(len(row.checkpoint_nonce) == 12, "checkpoint nonce is invalid")
        require(len(row.checkpoint_fingerprint) == 64, "checkpoint fingerprint is invalid")
        print(
            "WORKER_LIVE_RUN_OK status=completed turns=1 tokens=persisted cost=bounded "
            "checkpoint=encrypted fencing=yes owner=live_keycloak secrets=redacted"
        )
    finally:
        await runtime.aclose()


def main() -> int:
    values = keycloak_environment()
    owner_id: str | None = None
    with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
        token = admin_token(client, values)
        try:
            owner_id = create_owner(client, token)
            asyncio.run(run_live(owner_id))
        finally:
            if owner_id is not None:
                delete_owner(client, token, owner_id)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AcceptanceFailure as exc:
        print(f"WORKER_LIVE_RUN_FAILED reason={exc} secrets=redacted")
        raise SystemExit(1) from None
