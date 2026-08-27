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

