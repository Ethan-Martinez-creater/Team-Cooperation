"""KMS/Vault key resolution and rotation orchestration boundaries.

This package deliberately does not read key material from environment variables.
"""

from .core import (
    Actor,
    ExistingKeyringAdapter,
    FakeKeyProvider,
    KeyCache,
    KeyManagementError,
    KeyPurpose,
    KeyReference,
    ReencryptResult,
    RotationOrchestrator,
    RotationPlan,
    RotationStage,
)

__all__ = [
    "Actor", "ExistingKeyringAdapter", "FakeKeyProvider", "KeyCache",
    "KeyManagementError", "KeyPurpose", "KeyReference", "ReencryptResult",
    "RotationOrchestrator", "RotationPlan", "RotationStage",
]
