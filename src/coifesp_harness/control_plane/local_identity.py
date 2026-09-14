from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..auth import VerifiedIdentity
from ..errors import AuthenticationError
from ..security import Classification, Principal


@dataclass(frozen=True, slots=True)
class LocalProfile:
    profile_id: str
    display_name: str
    team_name: str
    principal: Principal


class LocalIdentityProvider:
    """In-process identities for local product demos; production always uses OIDC."""

    def __init__(self, *, session_hours: int = 12) -> None:
        self.session_hours = session_hours
        self.profiles = {
            item.profile_id: item
            for item in (
                LocalProfile(
                    "lead", "林澈 · 项目主导", "产品团队",
                    Principal("lead-lin", "team-product", frozenset({
                        "lead", "reviewer", "collaboration_creator",
                        "agent_run_controller", "artifact_publisher", "tool_approver",
                    }), Classification.RESTRICTED, frozenset({"demo-project"})),
                ),
                LocalProfile(
                    "contributor", "周宁 · 开发配合", "工程团队",
                    Principal("contributor-zhou", "team-engineering",
                        frozenset({
                            "contributor", "artifact_publisher",
                            "agent_run_controller", "tool_approver",
                        }),
                        Classification.CONFIDENTIAL, frozenset({"demo-project"})),
                ),
                LocalProfile(
                    "reviewer", "苏禾 · 独立评审", "质量团队",
                    Principal("reviewer-su", "team-quality",
                        frozenset({"reviewer", "tool_approver"}),
                        Classification.CONFIDENTIAL, frozenset({"demo-project"})),
                ),
            )
        }
        self._sessions: dict[str, VerifiedIdentity] = {}

    def issue(self, profile_id: str) -> tuple[str, VerifiedIdentity]:
        profile = self.profiles.get(profile_id)
        if profile is None:
            raise AuthenticationError("unknown local profile")
        token = secrets.token_urlsafe(32)
        identity = VerifiedIdentity(
            principal=profile.principal,
            issuer="coifesp-local",
            audience="coifesp-workspace",
            expires_at=datetime.now(UTC) + timedelta(hours=self.session_hours),
            token_id=None,
        )
        self._sessions[token] = identity
        return token, identity

    async def verify(self, token: str) -> VerifiedIdentity:
        identity = self._sessions.get(token)
        if identity is None or identity.expires_at <= datetime.now(UTC):
            self._sessions.pop(token, None)
            raise AuthenticationError("invalid local session")
        return identity

    def renew(self, token: str) -> tuple[str, VerifiedIdentity]:
        """Rotate a live local session, revoking the old token in the same step."""
        identity = self._sessions.get(token)
        if identity is None or identity.expires_at <= datetime.now(UTC):
            self._sessions.pop(token, None)
            raise AuthenticationError("invalid local session")
        new_token = secrets.token_urlsafe(32)
        renewed = VerifiedIdentity(
            principal=identity.principal,
            issuer=identity.issuer,
            audience=identity.audience,
            expires_at=datetime.now(UTC) + timedelta(hours=self.session_hours),
            token_id=None,
        )
        self._sessions.pop(token, None)
        self._sessions[new_token] = renewed
        return new_token, renewed

    def revoke(self, token: str) -> None:
        self._sessions.pop(token, None)

    async def aclose(self) -> None:
        self._sessions.clear()


class LocalPrincipalResolver:
    """Resolve the same fixed demo principals used by local browser sessions."""

    def __init__(self) -> None:
        self._provider = LocalIdentityProvider()

    async def resolve(self, *, tenant_id: str, principal_id: str) -> Principal:
        matches = [
            profile.principal
            for profile in self._provider.profiles.values()
            if profile.principal.tenant_id == tenant_id
            and profile.principal.principal_id == principal_id
        ]
        if len(matches) != 1:
            raise AuthenticationError("unknown local principal")
        return matches[0]

    async def aclose(self) -> None:
        await self._provider.aclose()
