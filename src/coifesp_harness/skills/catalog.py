from __future__ import annotations

import base64
import binascii
import hashlib
import re
from pathlib import Path
from typing import Any

import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from packaging.version import Version

from ..errors import SkillError, SkillIntegrityError
from ..security.models import Classification, Principal
from ..security.policy import DecisionEffect, PolicyEngine
from .models import SkillManifest, VerifiedSkill

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")
_TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)" r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_FRONTMATTER_RE = re.compile(
    r"\A---\r?\n(?P<metadata>.*?)\r?\n---\r?\n(?P<body>.*)\Z",
    re.DOTALL,
)
_ALLOWED_FIELDS = {
    "name",
    "version",
    "description",
    "tenant_id",
    "classification",
    "compartments",
    "required_tools",
    "signer_key_id",
}
_MAX_SKILL_BYTES = 1_000_000
_MAX_SIGNATURE_BYTES = 512


class SkillTrustStore:
    """Tenant-scoped signing roots. A key trusted by one tenant is not global."""

    def __init__(self) -> None:
        self._keys: dict[tuple[str, str], Ed25519PublicKey] = {}

    def register(
        self,
        *,
        tenant_id: str,
        key_id: str,
        public_key: Ed25519PublicKey,
    ) -> None:
        if not tenant_id or not _KEY_ID_RE.fullmatch(key_id):
            raise SkillError("tenant_id and a valid key_id are required")
        key = (tenant_id, key_id)
        if key in self._keys:
            raise SkillError("skill signing key is already registered")
        if not isinstance(public_key, Ed25519PublicKey):
            raise SkillError("skill signing key must be Ed25519")
        self._keys[key] = public_key

    def get(self, tenant_id: str, key_id: str) -> Ed25519PublicKey | None:
        return self._keys.get((tenant_id, key_id))


class SkillCatalog:
    def __init__(
        self,
        *,
        root: Path,
        trust_store: SkillTrustStore,
        policy: PolicyEngine,
    ) -> None:
        self.root = root.resolve()
        self.trust_store = trust_store
        self.policy = policy
        self._skills: dict[tuple[str, str, str], VerifiedSkill] = {}

    def scan(self) -> int:
        if not self.root.is_dir():
            raise SkillError("skill catalog root does not exist")
        discovered: dict[tuple[str, str, str], VerifiedSkill] = {}
        for manifest_path in sorted(self.root.rglob("SKILL.md")):
            skill = self._load_package(manifest_path)
            key = (
                skill.manifest.tenant_id,
                skill.manifest.name,
                skill.manifest.version,
            )
            if key in discovered:
                raise SkillError(f"duplicate skill package: {key}")
            discovered[key] = skill
        self._skills = discovered
        return len(discovered)

    def list_visible(self, principal: Principal) -> tuple[SkillManifest, ...]:
        visible = [
            skill.manifest for skill in self._skills.values() if self._can_read(principal, skill)
        ]
        return tuple(
            sorted(
                visible,
                key=lambda item: (item.name, _semver_key(item.version)),
            )
        )

    def load(
        self,
        *,
        principal: Principal,
        name: str,
        available_tools: frozenset[str],
        version: str | None = None,
    ) -> VerifiedSkill:
        candidates = [
            skill
            for (tenant_id, skill_name, skill_version), skill in self._skills.items()
            if tenant_id == principal.tenant_id
            and skill_name == name
            and (version is None or skill_version == version)
        ]
        if not candidates:
            raise SkillError("skill not found in the principal tenant")
        skill = max(candidates, key=lambda item: _semver_key(item.manifest.version))
        if not self._can_read(principal, skill):
            raise SkillError("principal is not allowed to read this skill")
        unavailable = skill.manifest.required_tools.difference(available_tools)
        if unavailable:
            raise SkillError(
                f"skill 依赖未授权的工具：{', '.join(sorted(unavailable))}"
            )
        return skill

    def _can_read(self, principal: Principal, skill: VerifiedSkill) -> bool:
        decision = self.policy.decide_resource_access(
            principal=principal,
            action=self._read_action(),
            resource=skill.manifest.resource_label,
        )
        return decision.effect is DecisionEffect.PERMIT

    @staticmethod
    def _read_action():
        from ..security.models import Action

        return Action.READ

    def _load_package(self, manifest_path: Path) -> VerifiedSkill:
        self._assert_safe_path(manifest_path)
        signature_path = manifest_path.with_name("SKILL.sig")
        self._assert_safe_path(signature_path)
        if not signature_path.is_file():
            raise SkillIntegrityError(f"missing detached signature: {signature_path.name}")

        raw = manifest_path.read_bytes()
        if not raw or len(raw) > _MAX_SKILL_BYTES:
            raise SkillError("skill package size is invalid")
        if b"\x00" in raw:
            raise SkillError("skill package contains a NUL byte")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillError("SKILL.md must be UTF-8") from exc
        manifest, body = _parse_manifest(text)
        self._assert_directory_identity(manifest_path, manifest)

        signature_raw = signature_path.read_bytes()
        if not signature_raw or len(signature_raw) > _MAX_SIGNATURE_BYTES:
            raise SkillIntegrityError("skill signature size is invalid")
        try:
            signature = base64.b64decode(signature_raw.strip(), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise SkillIntegrityError("skill signature is not valid base64") from exc
        public_key = self.trust_store.get(manifest.tenant_id, manifest.signer_key_id)
        if public_key is None:
            raise SkillIntegrityError("skill signer is not trusted for this tenant")
        try:
            public_key.verify(signature, raw)
        except InvalidSignature as exc:
            raise SkillIntegrityError("skill signature verification failed") from exc

        return VerifiedSkill(
            manifest=manifest,
            instructions=body.strip(),
            content_digest=hashlib.sha256(raw).hexdigest(),
            package_path=manifest_path.parent,
        )

    def _assert_safe_path(self, path: Path) -> None:
        try:
            resolved = path.resolve(strict=False)
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise SkillError("skill path escapes catalog root") from exc
        current = path
        while current != self.root and current != current.parent:
            if current.is_symlink():
                raise SkillError("symbolic links are not allowed in skill packages")
            current = current.parent

    @staticmethod
    def _assert_directory_identity(path: Path, manifest: SkillManifest) -> None:
        try:
            tenant_dir, name_dir, version_dir = path.parents[2], path.parents[1], path.parent
        except IndexError as exc:
            raise SkillError("skill path must be tenant/name/version/SKILL.md") from exc
        if (
            tenant_dir.name != manifest.tenant_id
            or name_dir.name != manifest.name
            or version_dir.name != manifest.version
        ):
            raise SkillError("skill manifest identity does not match its directory")


def _parse_manifest(text: str) -> tuple[SkillManifest, str]:
    match = _FRONTMATTER_RE.fullmatch(text)
    if match is None:
        raise SkillError("SKILL.md requires YAML frontmatter")
    try:
        metadata = yaml.safe_load(match.group("metadata"))
    except yaml.YAMLError as exc:
        raise SkillError("skill frontmatter is invalid YAML") from exc
    if not isinstance(metadata, dict):
        raise SkillError("skill frontmatter must be an object")
    unknown = set(metadata).difference(_ALLOWED_FIELDS)
    if unknown:
        raise SkillError(f"unknown skill manifest fields: {sorted(unknown)}")
    missing = _ALLOWED_FIELDS.difference(metadata)
    if missing:
        raise SkillError(f"missing skill manifest fields: {sorted(missing)}")

    name = _required_string(metadata, "name")
    version = _required_string(metadata, "version")
    description = _required_string(metadata, "description")
    tenant_id = _required_string(metadata, "tenant_id")
    signer_key_id = _required_string(metadata, "signer_key_id")
    if not _NAME_RE.fullmatch(name):
        raise SkillError("skill name must use lowercase kebab-case")
    if not _SEMVER_RE.fullmatch(version):
        raise SkillError("skill version must be semantic versioning")
    if len(description) > 1024:
        raise SkillError("skill description exceeds 1024 characters")
    if not _TENANT_RE.fullmatch(tenant_id):
        raise SkillError("skill tenant_id is invalid")
    if not _KEY_ID_RE.fullmatch(signer_key_id):
        raise SkillError("skill signer_key_id is invalid")
    try:
        classification = Classification[_required_string(metadata, "classification").upper()]
    except KeyError as exc:
        raise SkillError("skill classification is invalid") from exc
    compartments = _string_set(metadata, "compartments")
    required_tools = _string_set(metadata, "required_tools")
    for compartment in compartments:
        if not _IDENTIFIER_RE.fullmatch(compartment):
            raise SkillError(f"invalid compartment name: {compartment}")
    for tool_name in required_tools:
        if not _TOOL_NAME_RE.fullmatch(tool_name):
            raise SkillError(f"invalid required tool name: {tool_name}")
    body = match.group("body")
    if not body.strip():
        raise SkillError("skill instructions cannot be empty")
    return (
        SkillManifest(
            name=name,
            version=version,
            description=description,
            tenant_id=tenant_id,
            classification=classification,
            compartments=compartments,
            required_tools=required_tools,
            signer_key_id=signer_key_id,
        ),
        body,
    )


def _required_string(metadata: dict[str, Any], name: str) -> str:
    value = metadata.get(name)
    if not isinstance(value, str) or not value.strip():
        raise SkillError(f"{name} must be a non-empty string")
    return value.strip()


def _string_set(metadata: dict[str, Any], name: str) -> frozenset[str]:
    value = metadata.get(name)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise SkillError(f"{name} must be an array of non-empty strings")
    return frozenset(item.strip() for item in value)


def _semver_key(version: str) -> Version:
    if _SEMVER_RE.fullmatch(version) is None:
        raise SkillError("invalid semantic version")
    return Version(version)
