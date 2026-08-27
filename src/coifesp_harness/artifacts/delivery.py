from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from ..errors import IntegrityError, PolicyDenied
from .repository import SQLAlchemyArtifactRepository

_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class ArtifactDeliveryGuard:
    def __init__(self, repository: SQLAlchemyArtifactRepository) -> None:
        self.repository = repository

    def assert_assignment_artifacts(self, connection, *, principal, board,
                                    assignment_id: str,
                                    artifact_refs: tuple[str, ...]) -> None:
        assignment = board.assignments.get(assignment_id)
        if assignment is None:
            raise PolicyDenied("assignment is unavailable")
        for reference in artifact_refs:
            owner, artifact_id, digest = self.parse_reference(reference)
            manifest = self.repository.get(connection, principal=principal,
                owner_tenant_id=owner, artifact_id=artifact_id)
            if manifest.sha256 != digest:
                raise IntegrityError("artifact reference digest does not match manifest")
            if not assignment.visible_to_tenants.issubset(manifest.visible_to_tenants):
                raise PolicyDenied("artifact is not visible to every assignment participant")
            if manifest.label.classification > board.classification:
                raise PolicyDenied("artifact classification exceeds collaboration program")
            if not manifest.label.compartments.issubset(board.compartments):
                raise PolicyDenied("artifact compartments exceed collaboration program")

    @staticmethod
    def parse_reference(value: str) -> tuple[str, str, str]:
        parsed = urlsplit(value)
        query = parse_qs(parsed.query, strict_parsing=True)
        path = parsed.path.lstrip("/")
        digest = query.get("sha256", [])
        if (parsed.scheme != "artifact" or not parsed.netloc or not path
                or parsed.fragment or set(query) != {"sha256"} or len(digest) != 1
                or _DIGEST.fullmatch(digest[0]) is None):
            raise ValueError("artifact reference must bind owner, ID, and SHA-256")
        return parsed.netloc, path, digest[0]
