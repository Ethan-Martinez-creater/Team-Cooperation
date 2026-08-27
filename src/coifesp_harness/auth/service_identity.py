from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlparse

import httpx

from ..config import SecretValue
from ..errors import AuthenticationError, IdentityProviderUnavailable, IntegrityError, PolicyDenied
from ..security import Classification, Principal
from .oidc import OIDCVerifier
from .roles import APPLICATION_ROLES


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")
_ATTRIBUTE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class ClientCredentialsConfig:
    token_endpoint: str
    client_id: str
    client_secret: SecretValue
    scopes: tuple[str, ...] = ()
    timeout_seconds: float = 5.0
    refresh_skew_seconds: int = 30
    allow_insecure_http: bool = False

    def validate(self) -> None:
        parsed = urlparse(self.token_endpoint)
        if (
            parsed.scheme not in ({"http", "https"} if self.allow_insecure_http else {"https"})
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("OAuth token endpoint is invalid")
        if not _IDENTIFIER.fullmatch(self.client_id):
            raise ValueError("OAuth client id is invalid")
        if len(self.client_secret.reveal().encode("utf-8")) < 16:
            raise ValueError("OAuth client secret is too short")
        if not 0 < self.timeout_seconds <= 60:
            raise ValueError("OAuth timeout is invalid")
        if not 0 <= self.refresh_skew_seconds <= 300:
            raise ValueError("OAuth refresh skew is invalid")
        if len(self.scopes) > 32 or any(not _ATTRIBUTE.fullmatch(value) for value in self.scopes):
            raise ValueError("OAuth scopes are invalid")


@dataclass(frozen=True, slots=True)
class AccessToken:
    value: SecretValue
    expires_at_monotonic: float


class ClientCredentialsTokenProvider:
    """Concurrency-safe, bounded OAuth client-credentials token cache."""

    def __init__(
        self,
        config: ClientCredentialsConfig,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        config.validate()
        self.config = config
        self._clock = clock
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(config.timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            headers={"Accept": "application/json", "User-Agent": "coifesp-worker/0.1"},
        )
        self._owns_client = client is None
        self._cached: AccessToken | None = None
        self._lock = asyncio.Lock()

    async def token(self) -> SecretValue:
        cached = self._cached
        if cached is not None and self._usable(cached):
            return cached.value
        async with self._lock:
            cached = self._cached
            if cached is not None and self._usable(cached):
                return cached.value
            token = await self._request()
            self._cached = token
            return token.value

    def invalidate(self) -> None:
        self._cached = None

    async def aclose(self) -> None:
        self._cached = None
        if self._owns_client:
            await self._client.aclose()

    def _usable(self, token: AccessToken) -> bool:
        return self._clock() + self.config.refresh_skew_seconds < token.expires_at_monotonic

    async def _request(self) -> AccessToken:
        form = {
            "grant_type": "client_credentials",
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret.reveal(),
        }
        if self.config.scopes:
            form["scope"] = " ".join(self.config.scopes)
        try:
            response = await self._client.post(self.config.token_endpoint, data=form)
        except httpx.HTTPError as exc:
            raise IdentityProviderUnavailable("OAuth token endpoint is unavailable") from exc
        if response.status_code in {400, 401, 403}:
            raise AuthenticationError("invalid_client")
        if response.status_code != 200:
            raise IdentityProviderUnavailable("OAuth token endpoint returned an invalid status")
        payload = _bounded_json(response, operation="OAuth token grant")
        value = payload.get("access_token")
        token_type = payload.get("token_type")
        expires_in = payload.get("expires_in")
        if (
            not isinstance(value, str)
            or not 16 <= len(value) <= 65_536
            or not isinstance(token_type, str)
            or token_type.lower() != "bearer"
            or type(expires_in) is not int
            or not 30 <= expires_in <= 86_400
        ):
            raise IdentityProviderUnavailable("OAuth token response is invalid")
        return AccessToken(SecretValue(value), self._clock() + expires_in)


class OIDCWorkerIdentityProvider:
    """Obtains and verifies the worker token before privileged lease operations."""

    def __init__(
        self,
        *,
        tokens: ClientCredentialsTokenProvider,
        verifier: OIDCVerifier,
        required_role: str = "agent_worker",
        expected_tenant_id: str | None = None,
    ) -> None:
        self.tokens = tokens
        self.verifier = verifier
        self.required_role = required_role
        self.expected_tenant_id = expected_tenant_id

    async def resolve(self) -> Principal:
        raw = await self.tokens.token()
        try:
            identity = await self.verifier.verify(raw.reveal())
        except AuthenticationError:
            self.tokens.invalidate()
            raise
        principal = identity.principal
        if not principal.is_service or self.required_role not in principal.roles:
            raise PolicyDenied("worker token lacks the required service role")
        if self.expected_tenant_id is not None and principal.tenant_id != self.expected_tenant_id:
            raise IntegrityError("worker token tenant does not match worker configuration")
        return principal


@dataclass(frozen=True, slots=True)
class KeycloakDirectoryConfig:
    admin_api_base_url: str
    realm: str
    timeout_seconds: float = 5.0
    allow_insecure_http: bool = False

    def validate(self) -> None:
        parsed = urlparse(self.admin_api_base_url)
        if (
            parsed.scheme not in ({"http", "https"} if self.allow_insecure_http else {"https"})
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Keycloak Admin API base URL is invalid")
        if not _ATTRIBUTE.fullmatch(self.realm):
            raise ValueError("Keycloak realm is invalid")
        if not 0 < self.timeout_seconds <= 60:
            raise ValueError("Keycloak directory timeout is invalid")


class KeycloakPrincipalResolver:
    """Resolves current user attributes using a separately scoped directory client."""

    def __init__(
        self,
        *,
        config: KeycloakDirectoryConfig,
        tokens: ClientCredentialsTokenProvider,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        config.validate()
        self.config = config
        self.tokens = tokens
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(config.timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            headers={"Accept": "application/json", "User-Agent": "coifesp-worker/0.1"},
        )
        self._owns_client = client is None

    async def resolve(self, *, tenant_id: str, principal_id: str) -> Principal:
        if not _IDENTIFIER.fullmatch(tenant_id) or not _IDENTIFIER.fullmatch(principal_id):
            raise IntegrityError("durable run owner identity is invalid")
        token = await self.tokens.token()
        user_url = self._url(f"users/{quote(principal_id, safe='')}")
        user = await self._get(user_url, token)
        if user.get("id") != principal_id:
            raise IntegrityError("identity provider returned a mismatched principal")
        if user.get("enabled") is not True or user.get("serviceAccountClientId") is not None:
            raise PolicyDenied("durable run owner is disabled or is not a human identity")
        attributes = user.get("attributes", {})
        if not isinstance(attributes, Mapping):
            raise PolicyDenied("durable run owner has invalid direct security attributes")
        groups = await self._get(
            f"{user_url}/groups?briefRepresentation=false&first=0&max=129",
            token,
        )
        if not isinstance(groups, list) or len(groups) > 128:
            raise IdentityProviderUnavailable("Keycloak group response is invalid")
        attributes = _effective_attributes(attributes, groups)
        current_tenant = _single_attribute(attributes, "tenant_id")
        if current_tenant != tenant_id:
            raise IntegrityError("durable run owner tenant changed or mismatched")
        clearance_value = _single_attribute(attributes, "clearance")
        try:
            clearance = Classification[clearance_value.upper()]
        except KeyError as exc:
            raise PolicyDenied("durable run owner clearance is invalid") from exc
        compartments = _attribute_set(attributes, "compartments", required=False)
        roles_payload = await self._get(f"{user_url}/role-mappings/realm/composite", token)
        if not isinstance(roles_payload, list) or len(roles_payload) > 512:
            raise IdentityProviderUnavailable("Keycloak role response is invalid")
        roles = frozenset(
            role["name"]
            for role in roles_payload
            if isinstance(role, Mapping)
            and isinstance(role.get("name"), str)
            and _ATTRIBUTE.fullmatch(role["name"])
        ) & APPLICATION_ROLES
        if not roles:
            raise PolicyDenied("durable run owner has no current roles")
        return Principal(principal_id, current_tenant, roles, clearance, compartments, False)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _url(self, suffix: str) -> str:
        base = self.config.admin_api_base_url.rstrip("/")
        realm = quote(self.config.realm, safe="")
        return f"{base}/admin/realms/{realm}/{suffix}"

    async def _get(self, url: str, token: SecretValue) -> Any:
        try:
            response = await self._client.get(
                url, headers={"Authorization": f"Bearer {token.reveal()}"}
            )
        except httpx.HTTPError as exc:
            raise IdentityProviderUnavailable("Keycloak directory is unavailable") from exc
        if response.status_code == 401:
            self.tokens.invalidate()
            raise IdentityProviderUnavailable("Keycloak directory credential was rejected")
        if response.status_code == 403:
            raise PolicyDenied("directory client lacks required read permissions")
        if response.status_code == 404:
            raise PolicyDenied("durable run owner is absent")
        if response.status_code != 200:
            raise IdentityProviderUnavailable("Keycloak directory returned an invalid status")
        return _bounded_json(response, operation="Keycloak directory lookup")


def _bounded_json(response: httpx.Response, *, operation: str) -> Any:
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > 1_048_576:
                raise IdentityProviderUnavailable(f"{operation} response is too large")
        except ValueError as exc:
            raise IdentityProviderUnavailable(f"{operation} content length is invalid") from exc
    if len(response.content) > 1_048_576:
        raise IdentityProviderUnavailable(f"{operation} response is too large")
    try:
        return response.json()
    except ValueError as exc:
        raise IdentityProviderUnavailable(f"{operation} response is invalid") from exc


def _attribute_set(
    attributes: Mapping[str, Any], name: str, *, required: bool = True
) -> frozenset[str]:
    value = attributes.get(name)
    if value is None and not required:
        return frozenset()
    if (
        not isinstance(value, list)
        or len(value) > 128
        or any(not isinstance(item, str) or not _ATTRIBUTE.fullmatch(item) for item in value)
    ):
        raise PolicyDenied(f"durable run owner {name} attribute is invalid")
    result = frozenset(value)
    if required and not result:
        raise PolicyDenied(f"durable run owner {name} attribute is absent")
    return result


def _single_attribute(attributes: Mapping[str, Any], name: str) -> str:
    values = _attribute_set(attributes, name)
    if len(values) != 1:
        raise PolicyDenied(f"durable run owner {name} attribute must be singular")
    return next(iter(values))


def _effective_attributes(
    direct: Mapping[str, Any], groups: list[Any]
) -> Mapping[str, list[str]]:
    effective: dict[str, set[str]] = {}
    for source in (direct, *(group.get("attributes", {}) for group in groups if isinstance(group, Mapping))):
        if not isinstance(source, Mapping):
            raise PolicyDenied("durable run owner group attributes are invalid")
        for name in ("tenant_id", "clearance", "compartments"):
            value = source.get(name)
            if value is None:
                continue
            if (
                not isinstance(value, list)
                or len(value) > 128
                or any(not isinstance(item, str) or not _ATTRIBUTE.fullmatch(item) for item in value)
            ):
                raise PolicyDenied(f"durable run owner {name} attribute is invalid")
            effective.setdefault(name, set()).update(value)
    return {name: sorted(values) for name, values in effective.items()}
