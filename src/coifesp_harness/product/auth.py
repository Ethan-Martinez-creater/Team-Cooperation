from __future__ import annotations

from ..auth import VerifiedIdentity
from ..security.models import Classification, Principal
from .service import ProductAccountService


class BuiltinAccountVerifier:
    """Adapts persistent product sessions to the control-plane bearer contract."""

    def __init__(self, accounts: ProductAccountService) -> None:
        self.accounts = accounts

    async def verify(self, token: str) -> VerifiedIdentity:
        session = self.accounts.authenticate_session(token)
        account = session.account
        # Workspace members launch Agents and use contributor-grade tools such
        # as the sandbox; approval and publishing stay separately gated roles.
        roles = {"artifact_publisher", "contributor"}
        if account.team_role.value in {"owner", "admin"}:
            roles.update({"collaboration_creator", "tool_approver"})
        return VerifiedIdentity(
            principal=Principal(
                principal_id=account.account_id,
                tenant_id=account.team_id,
                roles=frozenset(roles),
                clearance=Classification.INTERNAL,
                compartments=frozenset(),
            ),
            issuer="coifesp:builtin",
            audience="coifesp-workspace",
            expires_at=session.expires_at,
            token_id=None,
        )

    async def aclose(self) -> None:
        return None
