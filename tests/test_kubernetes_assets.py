from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_kubernetes_assets.py"


def _validator():
    spec = importlib.util.spec_from_file_location("check_kubernetes_assets", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_kubernetes_assets_pass_structural_validation() -> None:
    errors, renderer = _validator().validate(allow_placeholders=True)
    assert renderer
    assert errors == []


def test_kubernetes_assets_require_deployer_replacements() -> None:
    errors, _ = _validator().validate(allow_placeholders=False)
    assert any("deployment placeholder remains" in error for error in errors)
    assert any("placeholder digest" in error for error in errors)


def test_production_workers_use_matching_shared_tenant_pools() -> None:
    module = _validator()
    configmaps = {
        item["metadata"]["name"]: item.get("data", {})
        for item in module.source_documents()
        if item.get("kind") == "ConfigMap"
    }
    agent = configmaps["coifesp-agent-worker-config"]
    tool = configmaps["coifesp-tool-worker-config"]

    agent_pool = module.tenant_pool(agent["COIFESP_WORKER_TENANTS"])
    tool_pool = module.tenant_pool(tool["COIFESP_TOOL_WORKER_TENANTS"])

    assert len(agent_pool) >= 2
    assert set(agent_pool) == set(tool_pool)
    assert "COIFESP_WORKER_TENANT_ID" not in agent
    assert "COIFESP_TOOL_WORKER_TENANT_ID" not in tool
