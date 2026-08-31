import asyncio
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from test_bootstrap import StubVerifier, production_settings
from test_delivery_service import counts, delivery, process, setup_delivery

from coifesp_harness.control_plane.app import create_app
from coifesp_harness.control_plane.delivery_routes import build_delivery_router
from coifesp_harness.errors import (
    GovernanceConflictError,
    PolicyDenied,
    ResourceNotFound,
)


def application(value):
    app = FastAPI()

    async def auth(request: Request):
        return SimpleNamespace(principal=SimpleNamespace(
            principal_id=request.headers.get("x-test-actor", "lead-a"),
            is_service=request.headers.get("x-test-service") == "yes"))

    async def denied(request, error):
        code = 403 if isinstance(error, PolicyDenied) else 404 if isinstance(error, ResourceNotFound) else 409
        return JSONResponse(status_code=code, content={"detail": str(error)})

    for error in (PolicyDenied, ResourceNotFound, GovernanceConflictError):
        app.add_exception_handler(error, denied)
    app.include_router(build_delivery_router(authenticator=auth, service=value.delivery_service))
    return app


def test_http_accepts_actual_delivery_but_rejects_service_and_injected_pass(tmp_path):
    value = setup_delivery(tmp_path)
    app = application(value)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            path = "/v1/projects/project-a/processes/process-a"
            response = await client.get(path + "/deliveries")
            assert response.status_code == 200 and len(response.json()["deliveries"]) == 1
            row = response.json()["deliveries"][0]
            payload = {"expected_process_version": process(value).version, "expected_version": row["version"],
                "decision": "ACCEPT", "reason": "Reviewed", "idempotency_key": "http-accept"}
            url = path + "/deliveries/" + row["delivery_id"] + ":decide"
            response = await client.post(url, json=payload, headers={"x-test-service": "yes"})
            assert response.status_code == 403 and counts(value) == (0, 0)
            response = await client.post(url, json={**payload, "passed": True})
            assert response.status_code == 422 and counts(value) == (0, 0)
            response = await client.post(url, json=payload)
            assert response.status_code == 200 and response.json()["status"] == "ACCEPTED"

    asyncio.run(scenario())
    assert process(value).status.value == "COMPLETED"


def test_http_cross_project_and_disabled_or_unknown_account_cannot_access(tmp_path):
    value = setup_delivery(tmp_path)
    app = application(value)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/v1/projects/other-project/processes/process-a/deliveries")
            assert response.status_code == 404
            response = await client.get("/v1/projects/project-a/processes/process-a/deliveries",
                headers={"x-test-actor": "unknown-account"})
            assert response.status_code == 403

    asyncio.run(scenario())
    assert counts(value) == (0, 0)


def test_app_mounts_delivery_service_without_starting_external_services(tmp_path):
    value = setup_delivery(tmp_path)
    app = create_app(settings=production_settings(), verifier=StubVerifier(), delivery_service=value.delivery_service)
    assert app.state.delivery_service is value.delivery_service
    paths = app.openapi()["paths"]
    assert "/v1/projects/{project_id}/processes/{process_id}/deliveries/{delivery_id}:decide" in paths
    assert delivery(value)["status"] == "READY"
