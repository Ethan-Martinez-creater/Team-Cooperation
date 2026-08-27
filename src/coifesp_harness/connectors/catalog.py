from __future__ import annotations

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
