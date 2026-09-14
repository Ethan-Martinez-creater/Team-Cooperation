import asyncio
import importlib.util
from pathlib import Path

import pytest

from coifesp_harness.control_plane.local_identity import LocalPrincipalResolver
from coifesp_harness.errors import AuthenticationError

ROOT = Path(__file__).resolve().parents[1]
START_LOCAL_WORKERS = ROOT / "scripts" / "start_local_workers.py"


def _local_worker_startup():
    spec = importlib.util.spec_from_file_location(
        "start_local_workers_for_test", START_LOCAL_WORKERS
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("tenant_id", "principal_id"),
    [
        ("team-product", "lead-lin"),
        ("team-engineering", "contributor-zhou"),
        ("team-quality", "reviewer-su"),
    ],
)
def test_local_worker_resolves_same_browser_principals(tenant_id, principal_id) -> None:
    resolver = LocalPrincipalResolver()
    principal = asyncio.run(
        resolver.resolve(tenant_id=tenant_id, principal_id=principal_id)
    )
    assert principal.tenant_id == tenant_id
    assert principal.principal_id == principal_id
    assert principal.is_service is False


def test_local_worker_rejects_cross_tenant_browser_principal() -> None:
    resolver = LocalPrincipalResolver()
    with pytest.raises(AuthenticationError, match="unknown local principal"):
        asyncio.run(
            resolver.resolve(
                tenant_id="team-quality", principal_id="contributor-zhou"
            )
        )


def test_local_worker_startup_uses_project_owned_artifact_store() -> None:
    startup = _local_worker_startup()
    environment = {"COIFESP_ARTIFACT_STORE_ROOT": r"E:\external-artifacts"}

    startup.configure_local_artifact_store(environment)

    expected = (startup.RUNTIME / "artifacts").resolve()
    assert Path(environment["COIFESP_ARTIFACT_STORE_ROOT"]) == expected
    assert expected.is_dir()
