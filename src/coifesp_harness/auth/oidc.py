from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import Message
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlparse

import httpx
import jwt

from ..config import Environment, Settings
from ..errors import AuthenticationError, IdentityProviderUnavailable
from ..security.models import Classification, Principal
from ..tls import explicit_ca_context
from .roles import APPLICATION_ROLES

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_DOCUMENT_BYTES = 1_048_576
_MAX_JWKS_KEYS = 64
_MIN_CACHE_SECONDS = 30
_MAX_CACHE_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class FetchResult:
    document: Mapping[str, Any]
    max_age_seconds: int


class JSONFetcher(Protocol):
    async def fetch(self, url: str) -> FetchResult: ...

    async def aclose(self) -> None: ...


class HttpxJSONFetcher:
    """Bounded HTTPS JSON fetcher for OIDC discovery and JWKS documents."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 5.0,
        allow_insecure_http: bool = False,
        tls_ca_bundle: str | None = None,
    ) -> None:
        self.allow_insecure_http = allow_insecure_http
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            verify=explicit_ca_context(tls_ca_bundle) or True,
            headers={
                "Accept": "application/json",
                "User-Agent": "coifesp-harness/0.1",
            },
        )

    async def fetch(self, url: str) -> FetchResult:
        self._validate_url(url)
        try:
            async with self._client.stream("GET", url) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                media_type = content_type.partition(";")[0].strip().lower()
                if not (media_type == "application/json" or media_type.endswith("+json")):
                    raise IdentityProviderUnavailable(
                        "identity provider returned a non-JSON document"
                    )
                raw_length = response.headers.get("content-length")
                if raw_length:
                    try:
                        if int(raw_length) > _MAX_DOCUMENT_BYTES:
                            raise IdentityProviderUnavailable(
                                "identity provider document exceeds the size limit"
                            )
                    except ValueError as exc:
                        raise IdentityProviderUnavailable(
                            "identity provider returned an invalid content length"
                        ) from exc
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > _MAX_DOCUMENT_BYTES:
                        raise IdentityProviderUnavailable(
                            "identity provider document exceeds the size limit"
                        )
                cache_control = response.headers.get("cache-control")
        except httpx.HTTPError as exc:
            raise IdentityProviderUnavailable("identity provider document fetch failed") from exc
        try:
            document = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IdentityProviderUnavailable("identity provider returned invalid JSON") from exc
        if not isinstance(document, dict):
            raise IdentityProviderUnavailable("identity provider JSON document must be an object")
        return FetchResult(
            document=document,
            max_age_seconds=_cache_max_age(cache_control),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _validate_url(self, url: str) -> None:
        parsed = urlparse(url)
        allowed_schemes = {"https"}
        if self.allow_insecure_http:
            allowed_schemes.add("http")
        if (
            parsed.scheme not in allowed_schemes
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise IdentityProviderUnavailable("identity provider document URL is not allowed")


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    principal: Principal
    issuer: str
    audience: str
    expires_at: datetime
    token_id: str | None


class OIDCVerifier:
    """Fail-closed OIDC JWT verifier with bounded discovery and JWKS caches."""

    def __init__(
        self,
        *,
        settings: Settings,
        fetcher: JSONFetcher | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        settings.validate(require_auth=True)
        assert settings.oidc_issuer is not None
        assert settings.oidc_audience is not None
        self.issuer = settings.oidc_issuer
        self.audience = settings.oidc_audience
        self.authorized_parties = settings.oidc_authorized_parties
        self.explicit_jwks_url = settings.oidc_jwks_url
        self.allowed_algorithms = frozenset(settings.oidc_algorithms)
        self.tenant_claim = settings.oidc_tenant_claim
        self.roles_claim = settings.oidc_roles_claim
        self.compartments_claim = settings.oidc_compartments_claim
        self.clearance_claim = settings.oidc_clearance_claim
        self.clock_skew_seconds = settings.oidc_clock_skew_seconds
        self.max_token_age_seconds = settings.oidc_max_token_age_seconds
        self._allow_insecure_http = settings.environment is not Environment.PRODUCTION
        self._fetcher = fetcher or HttpxJSONFetcher(
            allow_insecure_http=self._allow_insecure_http,
            tls_ca_bundle=settings.tls_ca_bundle,
        )
        self._owns_fetcher = fetcher is None
        self._clock = clock
        self._lock = asyncio.Lock()
        self._jwks_uri: str | None = self.explicit_jwks_url
        self._discovery_expires_at = 0.0
        self._keys: dict[str, jwt.PyJWK] = {}
        self._keys_expires_at = 0.0

    async def verify(self, token: str) -> VerifiedIdentity:
        if not token or len(token) > 8192:
            raise AuthenticationError("invalid_token")
        header = self._validated_header(token)
        key = await self._get_signing_key(header["kid"], header["alg"])
        try:
            claims = jwt.decode(
                token,
                key=key.key,
                algorithms=[header["alg"]],
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.clock_skew_seconds,
                options={
                    "require": ["exp", "iat", "iss", "aud", "sub"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_nbf": True,
                    "verify_iss": True,
                    "verify_aud": True,
                },
            )
        except jwt.PyJWTError as exc:
            raise AuthenticationError("invalid_token") from exc
        self._validate_temporal_bounds(claims)
        self._validate_authorized_party(claims)
        principal = self._map_principal(claims)
        return VerifiedIdentity(
            principal=principal,
            issuer=self.issuer,
            audience=self.audience,
            expires_at=datetime.fromtimestamp(int(claims["exp"]), tz=UTC),
            token_id=_optional_identifier(claims.get("jti")),
        )

    async def aclose(self) -> None:
        if self._owns_fetcher:
            await self._fetcher.aclose()

    def _validated_header(self, token: str) -> dict[str, str]:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise AuthenticationError("invalid_token") from exc
        algorithm = header.get("alg")
        key_id = header.get("kid")
        token_type = header.get("typ")
        critical = header.get("crit")
        if (
            not isinstance(algorithm, str)
            or algorithm not in self.allowed_algorithms
            or not isinstance(key_id, str)
            or not _KEY_ID.fullmatch(key_id)
        ):
            raise AuthenticationError("invalid_token")
        if token_type is not None and (
            not isinstance(token_type, str) or token_type.lower() not in {"jwt", "at+jwt"}
        ):
            raise AuthenticationError("invalid_token")
        if critical not in (None, []):
            raise AuthenticationError("invalid_token")
        return {"alg": algorithm, "kid": key_id}

    async def _get_signing_key(self, key_id: str, algorithm: str) -> jwt.PyJWK:
        async with self._lock:
            now = self._clock()
            key = self._keys.get(key_id)
            if key is not None and now < self._keys_expires_at:
                return self._validate_key(key, algorithm)
            await self._refresh_keys(force=key is None)
            key = self._keys.get(key_id)
            if key is None:
                raise AuthenticationError("invalid_token")
            return self._validate_key(key, algorithm)

    async def _refresh_keys(self, *, force: bool) -> None:
        now = self._clock()
        if not force and self._keys and now < self._keys_expires_at:
            return
        jwks_uri = await self._get_jwks_uri()
        result = await self._fetcher.fetch(jwks_uri)
        raw_keys = result.document.get("keys")
        if not isinstance(raw_keys, list) or not raw_keys or len(raw_keys) > _MAX_JWKS_KEYS:
            raise IdentityProviderUnavailable("JWKS key set is invalid")
        parsed_keys: dict[str, jwt.PyJWK] = {}
        for raw_key in raw_keys:
            if not isinstance(raw_key, dict):
                raise IdentityProviderUnavailable("JWKS contains an invalid key")
            key_id = raw_key.get("kid")
            if not isinstance(key_id, str) or not _KEY_ID.fullmatch(key_id):
                raise IdentityProviderUnavailable("JWKS key id is invalid")
            if key_id in parsed_keys:
                raise IdentityProviderUnavailable("JWKS contains duplicate key ids")
            if raw_key.get("use") not in (None, "sig"):
                continue
            key_ops = raw_key.get("key_ops")
            if key_ops is not None and (not isinstance(key_ops, list) or "verify" not in key_ops):
                continue
            try:
                parsed_keys[key_id] = jwt.PyJWK.from_dict(raw_key)
            except (jwt.PyJWTError, ValueError) as exc:
                raise IdentityProviderUnavailable("JWKS key is invalid") from exc
        if not parsed_keys:
            raise IdentityProviderUnavailable("JWKS contains no usable signing keys")
        self._keys = parsed_keys
        self._keys_expires_at = now + _bounded_cache_age(result.max_age_seconds)

    async def _get_jwks_uri(self) -> str:
        now = self._clock()
        if self._jwks_uri is not None and (
            self.explicit_jwks_url is not None or now < self._discovery_expires_at
        ):
            return self._jwks_uri
        discovery_url = f"{self.issuer.rstrip('/')}/.well-known/openid-configuration"
        result = await self._fetcher.fetch(discovery_url)
        document_issuer = result.document.get("issuer")
        jwks_uri = result.document.get("jwks_uri")
        if document_issuer != self.issuer or not isinstance(jwks_uri, str):
            raise IdentityProviderUnavailable(
                "OIDC discovery metadata does not match configuration"
            )
        _validate_remote_url(
            jwks_uri,
            allow_insecure_http=self._allow_insecure_http,
        )
        self._jwks_uri = jwks_uri
        self._discovery_expires_at = now + _bounded_cache_age(result.max_age_seconds)
        return jwks_uri

    def _validate_key(self, key: jwt.PyJWK, algorithm: str) -> jwt.PyJWK:
        if key.algorithm_name != algorithm:
            raise AuthenticationError("invalid_token")
        return key

    def _validate_temporal_bounds(self, claims: Mapping[str, Any]) -> None:
        try:
            issued_at = int(claims["iat"])
            expires_at = int(claims["exp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AuthenticationError("invalid_token") from exc
        if expires_at <= issued_at or expires_at - issued_at > self.max_token_age_seconds:
            raise AuthenticationError("invalid_token")

    def _validate_authorized_party(self, claims: Mapping[str, Any]) -> None:
        authorized_party = claims.get("azp")
        if authorized_party is None:
            raise AuthenticationError("invalid_token")
        if not isinstance(authorized_party, str) or not _IDENTIFIER.fullmatch(
            authorized_party
        ):
            raise AuthenticationError("invalid_token")
        if authorized_party not in self.authorized_parties:
            raise AuthenticationError("invalid_token")

    def _map_principal(self, claims: Mapping[str, Any]) -> Principal:
        subject = _required_identifier(claims.get("sub"))
        tenant_id = _required_identifier(claims.get(self.tenant_claim))
        roles = _required_identifier_set(claims.get(self.roles_claim)) & APPLICATION_ROLES
        if not roles:
            raise AuthenticationError("invalid_token")
        compartments = _required_identifier_set(claims.get(self.compartments_claim))
        raw_clearance = claims.get(self.clearance_claim)
        if not isinstance(raw_clearance, str):
            raise AuthenticationError("invalid_token")
        try:
            clearance = Classification[raw_clearance.strip().upper()]
        except KeyError as exc:
            raise AuthenticationError("invalid_token") from exc
        return Principal(
            principal_id=subject,
            tenant_id=tenant_id,
            roles=roles,
            clearance=clearance,
            compartments=compartments,
            is_service=claims.get("token_use") == "service",
        )


def _required_identifier(value: Any) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise AuthenticationError("invalid_token")
    return value


def _optional_identifier(value: Any) -> str | None:
    if value is None:
        return None
    return _required_identifier(value)


def _required_identifier_set(value: Any) -> frozenset[str]:
    if (
        not isinstance(value, list)
        or len(value) > 64
        or any(not isinstance(item, str) or not _IDENTIFIER.fullmatch(item) for item in value)
    ):
        raise AuthenticationError("invalid_token")
    return frozenset(value)


def _cache_max_age(cache_control: str | None) -> int:
    if not cache_control:
        return 300
    message = Message()
    message["cache-control"] = cache_control
    for directive in message.get("cache-control", "").split(","):
        name, separator, raw_value = directive.strip().partition("=")
        if name.lower() == "max-age" and separator:
            try:
                return int(raw_value.strip().strip('"'))
            except ValueError:
                return 300
    return 300


def _bounded_cache_age(value: int) -> int:
    return max(_MIN_CACHE_SECONDS, min(value, _MAX_CACHE_SECONDS))


def _validate_remote_url(url: str, *, allow_insecure_http: bool) -> None:
    parsed = urlparse(url)
    allowed_schemes = {"https"}
    if allow_insecure_http:
        allowed_schemes.add("http")
    if (
        parsed.scheme not in allowed_schemes
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise IdentityProviderUnavailable("OIDC JWKS URL is not allowed")
