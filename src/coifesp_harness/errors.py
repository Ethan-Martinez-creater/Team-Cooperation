"""Typed failures used at security and execution boundaries."""


class HarnessError(Exception):
    """Base class for expected harness failures."""


class PolicyDenied(HarnessError):
    """The policy decision point denied an operation."""


class ApprovalRequired(HarnessError):
    """An operation is valid but requires an external approval."""


class IntegrityError(HarnessError):
    """A signed or hash-linked object failed integrity validation."""


class DuplicateRequest(HarnessError):
    """A request reused an idempotency key."""


class IdempotencyConflict(HarnessError):
    """An idempotency key was reused with a different request digest."""


class BudgetExceeded(HarnessError):
    """An agent run exceeded one of its hard budgets."""


class ContextAssemblyError(HarnessError):
    """Model context could not be assembled without violating a hard boundary."""


class GovernanceError(HarnessError):
    """A collaboration governance command violated role or state constraints."""


class GovernanceConflictError(GovernanceError):
    """A governance aggregate lost an optimistic concurrency race."""


class SkillError(HarnessError):
    """A skill package or skill access request is invalid."""


class SkillIntegrityError(SkillError):
    """A skill package signature or digest is invalid."""


class MemoryError(HarnessError):
    """A memory operation violates admission, scope, or lifecycle constraints."""


class MemoryIntegrityError(MemoryError):
    """Encrypted memory authentication or metadata binding failed."""


class MemoryUnavailableError(MemoryError):
    """A Memory record is absent or hidden from the requesting principal."""


class MemoryConflictError(MemoryError):
    """A Memory lifecycle update lost an optimistic concurrency race."""


class AuthenticationError(HarnessError):
    """A credential is missing, malformed, expired, or otherwise invalid."""

    def __init__(self, code: str = "invalid_token") -> None:
        super().__init__(code)
        self.code = code


class IdentityProviderUnavailable(HarnessError):
    """The configured identity provider could not be verified safely."""


class ResourceNotFound(HarnessError):
    """A resource is absent or intentionally hidden from the principal."""
