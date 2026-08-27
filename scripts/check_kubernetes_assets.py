from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
KUBERNETES_ROOT = REPO_ROOT / "deploy" / "kubernetes"
OVERLAY = KUBERNETES_ROOT / "overlays" / "production"
PLACEHOLDERS = ("example.invalid", "replace-me")
IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,254}@sha256:[0-9a-f]{64}$")
ZERO_DIGEST = re.compile(r"sha256:([0-9a-f])\1{63}$")
SECRETISH = re.compile(
    r"(?:SECRET|PASSWORD|TOKEN|DATABASE_URL|SIGNING_KEY|MASTER_KEY|API_KEY|KEYS_JSON)$"
)
WORKLOADS = {
    "coifesp-control-plane": "coifesp-control-plane",
    "coifesp-agent-worker": "coifesp-agent-worker",
    "coifesp-tool-worker": "coifesp-tool-worker",
}


def load_yaml_documents(path: Path) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for item in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(item, dict):
            documents.append(item)
    return documents


def source_documents() -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for path in sorted((KUBERNETES_ROOT / "base").glob("*.yaml")):
        if path.name != "kustomization.yaml":
            documents.extend(load_yaml_documents(path))
    return documents


def render_overlay() -> tuple[list[dict[str, Any]], str]:
    kubectl = shutil.which("kubectl")
    if kubectl:
        completed = subprocess.run(
            [kubectl, "kustomize", str(OVERLAY)],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if completed.returncode != 0:
            raise RuntimeError(f"kubectl kustomize failed: {completed.stderr.strip()}")
        return list(yaml.safe_load_all(completed.stdout)), "kubectl kustomize"
    return source_documents(), "source YAML (kubectl unavailable)"


def containers(pod_spec: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield from pod_spec.get("initContainers", [])
    yield from pod_spec.get("containers", [])


def validate(*, allow_placeholders: bool) -> tuple[list[str], str]:
    errors: list[str] = []
    source = source_documents()
    try:
        rendered, renderer = render_overlay()
    except (OSError, RuntimeError, yaml.YAMLError) as exc:
        return [str(exc)], "render failed"

    docs = [doc for doc in rendered if isinstance(doc, dict)]
    by_key = {
        (doc.get("kind"), doc.get("metadata", {}).get("name")): doc for doc in docs
    }
    manifest_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(KUBERNETES_ROOT.rglob("*.yaml"))
    )
    if ":latest" in manifest_text or "imagePullPolicy: Always" in manifest_text:
        errors.append("images must not use latest or an unconditionally mutable pull policy")
    if any(value in manifest_text for value in ("privileged: true", "hostPath:", "hostNetwork: true")):
        errors.append("privileged, hostPath, and hostNetwork are forbidden")
    if "/var/run/docker.sock" in manifest_text:
        errors.append("the host container-engine socket is forbidden")

    for placeholder in PLACEHOLDERS:
        if not allow_placeholders and placeholder in manifest_text:
            errors.append(f"deployment placeholder remains: {placeholder}")
    if not allow_placeholders and ZERO_DIGEST.search(manifest_text):
        errors.append("deployment placeholder digest remains")

    secret_env_names: set[str] = set()
    for document in source:
        kind = document.get("kind")
        if kind == "Secret":
            errors.append("Secret manifests must not be committed")
        if kind == "ConfigMap":
            for name, value in document.get("data", {}).items():
                if SECRETISH.search(name) and str(value).strip():
                    errors.append(f"ConfigMap {document['metadata']['name']} contains secret-like {name}")

    namespace = by_key.get(("Namespace", "coifesp"), {})
    labels = namespace.get("metadata", {}).get("labels", {})
    if labels.get("pod-security.kubernetes.io/enforce") != "restricted":
        errors.append("namespace must enforce the restricted Pod Security Standard")

    deployments: dict[str, dict[str, Any]] = {}
    for name, service_account in WORKLOADS.items():
        deployment = by_key.get(("Deployment", name))
        if not deployment:
            errors.append(f"missing Deployment/{name}")
            continue
        deployments[name] = deployment
        spec = deployment.get("spec", {}).get("template", {}).get("spec", {})
        if spec.get("serviceAccountName") != service_account:
            errors.append(f"Deployment/{name} uses the wrong service account")
        if spec.get("automountServiceAccountToken") is not False:
            errors.append(f"Deployment/{name} must disable service-account token mounting")
        if not spec.get("securityContext", {}).get("runAsNonRoot"):
            errors.append(f"Deployment/{name} must run as non-root")
        if spec.get("securityContext", {}).get("seccompProfile", {}).get("type") != "RuntimeDefault":
            errors.append(f"Deployment/{name} must use RuntimeDefault seccomp")
        if len(spec.get("topologySpreadConstraints", [])) < 2:
            errors.append(f"Deployment/{name} lacks zone and hostname spreading")
        if not isinstance(spec.get("terminationGracePeriodSeconds"), int):
            errors.append(f"Deployment/{name} lacks a termination grace period")
        for container in containers(spec):
            prefix = f"Deployment/{name} container/{container.get('name')}"
            image = container.get("image", "")
            if not IMAGE.fullmatch(image):
                errors.append(f"{prefix} image is not digest pinned")
            elif not allow_placeholders and ZERO_DIGEST.search(image):
                errors.append(f"{prefix} uses a placeholder digest")
            security = container.get("securityContext", {})
            if security.get("allowPrivilegeEscalation") is not False:
                errors.append(f"{prefix} permits privilege escalation")
            if security.get("readOnlyRootFilesystem") is not True:
                errors.append(f"{prefix} root filesystem is writable")
            if security.get("capabilities", {}).get("drop") != ["ALL"]:
                errors.append(f"{prefix} must drop all capabilities")
            resources = container.get("resources", {})
            if not resources.get("requests") or not resources.get("limits"):
                errors.append(f"{prefix} lacks resource requests/limits")
            for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
                if probe not in container:
                    errors.append(f"{prefix} lacks {probe}")
            for env in container.get("env", []):
                env_name = env.get("name", "")
                if SECRETISH.search(env_name) and "value" in env:
                    errors.append(f"{prefix} inlines secret-like env {env['name']}")
                if "secretKeyRef" in env.get("valueFrom", {}):
                    secret_env_names.add(env_name)

    required_secret_env = {
        "COIFESP_DATABASE_URL",
        "COIFESP_AUDIT_SIGNING_KEY",
        "COIFESP_ENVELOPE_SIGNING_KEY",
        "COIFESP_MEMORY_MASTER_KEY",
        "COIFESP_WORKER_CLIENT_SECRET",
        "COIFESP_DIRECTORY_CLIENT_SECRET",
        "COIFESP_LLM_API_KEY",
        "COIFESP_TOOL_WORKER_CLIENT_SECRET",
    }
    missing_secret_env = sorted(required_secret_env - secret_env_names)
    if missing_secret_env:
        errors.append("missing Secret references for: " + ", ".join(missing_secret_env))

    control = deployments.get("coifesp-control-plane", {})
    control_container = (
        control.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [{}])[0]
    )
    if control_container.get("readinessProbe", {}).get("httpGet", {}).get("path") != "/health/ready":
        errors.append("control-plane readiness must use /health/ready")
    if control_container.get("livenessProbe", {}).get("httpGet", {}).get("path") != "/health/live":
        errors.append("control-plane liveness must use /health/live")
    control_config = by_key.get(("ConfigMap", "coifesp-control-plane-config"), {}).get("data", {})
    if control_config.get("COIFESP_TELEMETRY_ENABLED") != "true":
        errors.append("control-plane production telemetry must be enabled")
    if not str(control_config.get("COIFESP_OTLP_TRACES_ENDPOINT", "")).startswith("https://"):
        errors.append("control-plane OTLP trace endpoint must use HTTPS")
    if control_config.get("COIFESP_METRICS_ENABLED") != "true":
        errors.append("control-plane production metrics must be enabled")
    control_env = {item.get("name"): item for item in control_container.get("env", [])}
    metrics_token = control_env.get("COIFESP_METRICS_BEARER_TOKEN", {})
    if "secretKeyRef" not in metrics_token.get("valueFrom", {}):
        errors.append("control-plane metrics bearer token must come from a Secret")

    tool_spec = (
        deployments.get("coifesp-tool-worker", {})
        .get("spec", {})
        .get("template", {})
        .get("spec", {})
    )
    if tool_spec.get("runtimeClassName") != "coifesp-sandbox":
        errors.append("Tool Worker must use the isolated coifesp-sandbox RuntimeClass")
    sidecars = {item.get("name"): item for item in tool_spec.get("initContainers", [])}
    if sidecars.get("sandbox-engine", {}).get("restartPolicy") != "Always":
        errors.append("Tool Worker sandbox engine must be a native sidecar")

    for name in WORKLOADS:
        if ("PodDisruptionBudget", name) not in by_key:
            errors.append(f"missing PodDisruptionBudget/{name}")
    deny = by_key.get(("NetworkPolicy", "default-deny"), {}).get("spec", {})
    if deny.get("podSelector") != {} or deny.get("ingress") != [] or deny.get("egress") != []:
        errors.append("default-deny NetworkPolicy is incomplete")

    role = by_key.get(("Role", "coifesp-runtime-no-access"), {})
    if role.get("rules") != []:
        errors.append("runtime RBAC Role must grant no Kubernetes API permissions")

    if ("HorizontalPodAutoscaler", "coifesp-control-plane") not in by_key:
        errors.append("control-plane HPA is missing")
    for name in ("coifesp-agent-worker", "coifesp-tool-worker"):
        if ("HorizontalPodAutoscaler", name) in by_key:
            errors.append(f"unsafe generic HPA found for durable {name}")

    keda_docs = load_yaml_documents(
        KUBERNETES_ROOT / "autoscaling" / "keda-claimable-backlog.example.yaml"
    )
    for item in keda_docs:
        if item.get("kind") != "ScaledObject":
            continue
        name = item.get("metadata", {}).get("name", "")
        scale_down = (
            item.get("spec", {})
            .get("advanced", {})
            .get("horizontalPodAutoscalerConfig", {})
            .get("behavior", {})
            .get("scaleDown", {})
        )
        if scale_down.get("selectPolicy") != "Disabled":
            errors.append(f"optional {name} must disable automatic scale-down")
        if item.get("spec", {}).get("minReplicaCount", 0) < 2:
            errors.append(f"optional {name} must keep at least two replicas")
        query = str(item.get("spec", {}).get("triggers", [{}])[0].get("metadata", {}).get("query", ""))
        if "claimable" not in query or "tenant_id=" not in query:
            errors.append(f"optional {name} metric is not tenant-scoped claimable backlog")

    return errors, renderer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the COIFESP Kubernetes baseline")
    parser.add_argument(
        "--allow-placeholders",
        action="store_true",
        help="allow documented deployer replacement values (for repository CI)",
    )
    args = parser.parse_args(argv)
    try:
        errors, renderer = validate(allow_placeholders=args.allow_placeholders)
    except (OSError, yaml.YAMLError) as exc:
        print(f"Kubernetes asset validation failed: {exc}", file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"Kubernetes assets valid ({renderer})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
