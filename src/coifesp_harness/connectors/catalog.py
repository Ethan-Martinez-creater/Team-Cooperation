from __future__ import annotations

from collections.abc import Mapping

from ..errors import ResourceNotFound
from .models import ConnectorEndpoint


class ConnectorCatalog:
    def __init__(self) -> None:
        self._values: dict[tuple[str, str], ConnectorEndpoint] = {}

    def register(self, endpoint: ConnectorEndpoint) -> None:
        key = (endpoint.tenant_id, endpoint.connector_id)
        if key in self._values:
            raise ValueError("connector is already registered for this tenant")
        self._values[key] = endpoint

    def get(self, *, tenant_id: str, connector_id: str) -> ConnectorEndpoint | None:
        return self._values.get((tenant_id, connector_id))

    def list_for_tenant(self, tenant_id: str) -> tuple[ConnectorEndpoint, ...]:
        return tuple(value for key, value in sorted(self._values.items()) if key[0] == tenant_id)


class ReviewedConnectorCatalog:
    """Resolve only deployment-allowed connector revisions approved in the control plane."""

    def __init__(
        self,
        *,
        registry,
        tenant_id: str | None = None,
        allowed_tenant_ids: frozenset[str] | None = None,
        allowed_connector_ids: frozenset[str] | None = None,
        allowed_pairs: frozenset[tuple[str, str]] | None = None,
        environment: Mapping[str, str],
    ) -> None:
        if allowed_tenant_ids is None:
            allowed_tenant_ids = frozenset({tenant_id}) if tenant_id else frozenset()
        elif tenant_id is not None and allowed_tenant_ids != frozenset({tenant_id}):
            raise ValueError("reviewed connector tenant scope conflicts")
        if allowed_pairs is None:
            if not allowed_tenant_ids or not allowed_connector_ids:
                raise ValueError("reviewed connector catalog requires a tenant and allowlist")
            # Backwards-compatible construction. Multi-tenant callers pass exact
            # deployment pairs so independent allowlists cannot form a Cartesian product.
            allowed_pairs = frozenset(
                (allowed_tenant_id, connector_id)
                for allowed_tenant_id in allowed_tenant_ids
                for connector_id in allowed_connector_ids
            )
        if not allowed_pairs:
            raise ValueError("reviewed connector catalog requires a tenant and allowlist")
        pair_tenants = frozenset(pair[0] for pair in allowed_pairs)
        if not pair_tenants.issubset(allowed_tenant_ids):
            raise ValueError("reviewed connector pair is outside the tenant scope")
        self.registry = registry
        self.tenant_id = tenant_id
        self.allowed_tenant_ids = allowed_tenant_ids
        self.allowed_connector_ids = frozenset(pair[1] for pair in allowed_pairs)
        self.allowed_pairs = allowed_pairs
        self.environment = environment

    def get(self, *, tenant_id: str, connector_id: str) -> ConnectorEndpoint | None:
        if (tenant_id, connector_id) not in self.allowed_pairs:
            return None
        try:
            return self.registry.active_endpoint_for_worker(
                tenant_id=tenant_id,
                connector_id=connector_id,
                environment=self.environment,
            )
        except ResourceNotFound:
            return None
