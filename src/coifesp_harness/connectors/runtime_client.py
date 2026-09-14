"""Select the connector transport explicitly; embedded GitHub needs no DNS or port."""

import os

import httpx

from ..config import ConfigurationError, Environment
from .client import SecureConnectorClient

EMBEDDED_GITHUB_ORIGIN = "https://coifesp-github-adapter.invalid"


def build_connector_client(
    *,
    catalog,
    runtime_environment,
    environment=None,
    github_tenant_ids=None,
):
    source = os.environ if environment is None else environment
    mode = source.get("COIFESP_GITHUB_ADAPTER_MODE", "external")
    if mode == "external":
        return SecureConnectorClient(catalog=catalog)
    if mode != "embedded":
        raise ConfigurationError("GitHub adapter mode must be external or embedded")
    if runtime_environment == Environment.PRODUCTION:
        raise ConfigurationError("Embedded GitHub adapter is for local development only")
    if github_tenant_ids is not None and len(frozenset(github_tenant_ids)) != 1:
        raise ConfigurationError(
            "Embedded GitHub adapter requires exactly one GitHub connector tenant"
        )

    from .github_adapter import GitHubAdapterSettings, create_app

    try:
        settings = GitHubAdapterSettings.from_env(source)
    except (KeyError, TypeError, ValueError):
        raise ConfigurationError("Embedded GitHub adapter configuration is incomplete") from None
    app = create_app(settings)

    def client_factory(**kwargs):
        # Only this fixed logical origin is routed in-process. Other reviewed
        # connectors retain their ordinary HTTPS transport and verification.
        return httpx.AsyncClient(mounts={
            EMBEDDED_GITHUB_ORIGIN: httpx.ASGITransport(app=app),
        }, **kwargs)

    return SecureConnectorClient(catalog=catalog, client_factory=client_factory)
