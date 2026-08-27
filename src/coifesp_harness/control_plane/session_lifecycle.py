from __future__ import annotations

import asyncio
import time
from typing import Callable
from urllib.parse import urlparse

from ..auth import HttpxJSONFetcher, JSONFetcher
from ..config import Environment, Settings
from ..errors import IdentityProviderUnavailable

_DISCOVERY_CACHE_SECONDS = 300
_MAX_ENDPOINT_LENGTH = 2048


class SessionLifecycleService:
    """OIDC session metadata for the browser (silent renewal + global logout).

    Only non-secret discovery values are exposed: the end-session endpoint and
    the logout redirect URI. Access and refresh tokens never touch this service.
    Discovery is cached briefly and the fetched endpoint is validated to be a
    credential-free HTTP(S) URL so a compromised document cannot redirect the
    browser to a custom scheme or an internal address.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        fetcher: JSONFetcher | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self._allow_insecure_http = settings.environment is not Environment.PRODUCTION
        self._fetcher = fetcher or HttpxJSONFetcher(
            allow_insecure_http=self._allow_insecure_http
        )
        self._owns_fetcher = fetcher is None
        self._clock = clock
        self._lock = asyncio.Lock()
        self._endpoint_cache: str | None = None
        self._discovery_expires_at = 0.0

    async def oidc_session_config(self, *, redirect_uri: str) -> dict:
        """Return safe OIDC session metadata for the workspace."""
        issuer = self.settings.oidc_issuer
        client_id = self.settings.ui_oidc_client_id
        if not issuer or not client_id:
            raise IdentityProviderUnavailable("workspace OIDC configuration is unavailable")
        end_session = await self._end_session_endpoint()
        return {
            "auth_mode": "oidc",
            "issuer": issuer.rstrip("/"),
            "client_id": client_id,
            "audience": self.settings.oidc_audience,
            "redirect_uri": redirect_uri,
            "end_session_endpoint": end_session,
            "post_logout_redirect_uri": redirect_uri,
        }

    async def _end_session_endpoint(self) -> str | None:
        if self.settings.oidc_issuer is None:
            return None
        now = self._clock()
        if self._endpoint_cache is not None and now < self._discovery_expires_at:
            return self._endpoint_cache
        async with self._lock:
            if self._endpoint_cache is not None and now < self._discovery_expires_at:
                return self._endpoint_cache
            document = await self._discovery_document()
            endpoint = document.get("end_session_endpoint")
            if endpoint is None:
                self._endpoint_cache = None
                self._discovery_expires_at = now + _DISCOVERY_CACHE_SECONDS
                return None
            if not isinstance(endpoint, str) or not _is_allowed_https_url(
                endpoint, allow_insecure_http=self._allow_insecure_http
            ):
                raise IdentityProviderUnavailable(
                    "identity provider returned an invalid end-session endpoint"
                )
            self._endpoint_cache = endpoint
            self._discovery_expires_at = now + _DISCOVERY_CACHE_SECONDS
            return endpoint

    async def _discovery_document(self) -> dict:
        issuer = self.settings.oidc_issuer
        assert issuer is not None
        result = await self._fetcher.fetch(f"{issuer.rstrip('/')}/.well-known/openid-configuration")
        document = result.document
        document_issuer = document.get("issuer")
        if not isinstance(document_issuer, str) or document_issuer.rstrip("/") != issuer.rstrip("/"):
            raise IdentityProviderUnavailable(
                "OIDC discovery metadata does not match configuration"
            )
        return document

    async def aclose(self) -> None:
        if self._owns_fetcher:
            await self._fetcher.aclose()


def _is_allowed_https_url(value: str, *, allow_insecure_http: bool) -> bool:
    if len(value) > _MAX_ENDPOINT_LENGTH:
        return False
    parsed = urlparse(value)
    allowed = {"https"}
    if allow_insecure_http:
        allowed.add("http")
    return (
        parsed.scheme in allowed
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )
