import asyncio
import json
from datetime import UTC, datetime

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from coifesp_harness.auth import FetchResult, OIDCVerifier
from coifesp_harness.config import ConfigurationError, Settings
from coifesp_harness.errors import (
    AuthenticationError,
    IdentityProviderUnavailable,
)
from coifesp_harness.security import Classification

ISSUER = "https://identity.example.test"
AUDIENCE = "coifesp-control-plane"
JWKS_URL = f"{ISSUER}/jwks"


class StubFetcher:
    def __init__(self, responses):
        self.responses = {
            url: list(value) if isinstance(value, list) else [value]
            for url, value in responses.items()
        }
        self.calls: list[str] = []
        self.closed = False

    async def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        responses = self.responses[url]
        response = responses.pop(0) if len(responses) > 1 else responses[0]
        if isinstance(response, Exception):
            raise response
        return response

    async def aclose(self) -> None:
        self.closed = True


def oidc_settings(**overrides) -> Settings:
    values = {
        "COIFESP_ENV": "test",
        "COIFESP_OIDC_ISSUER": ISSUER,
        "COIFESP_OIDC_AUDIENCE": AUDIENCE,
        "COIFESP_OIDC_AUTHORIZED_PARTIES": "coifesp-local-ui,coifesp-agent-worker",
        "COIFESP_OIDC_JWKS_URL": JWKS_URL,
        "COIFESP_OIDC_ALGORITHMS": "RS256",
        "COIFESP_OIDC_MAX_TOKEN_AGE_SECONDS": "3600",
    }
    values.update(overrides)
    return Settings.from_environment(values)


def key_material(key_id: str = "key-1"):
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": key_id, "use": "sig", "alg": "RS256"})
    return private_key, jwk


def claims(**overrides):
    now = int(datetime.now(UTC).timestamp())
    value = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "user-123",
        "iat": now,
        "exp": now + 300,
        "jti": "token-123",
        "tenant_id": "tenant-a",
        "roles": ["contributor"],
        "compartments": ["project-x"],
        "clearance": "confidential",
        "azp": "coifesp-local-ui",
    }
    value.update(overrides)
    return value


def signed_token(private_key, payload=None, *, key_id="key-1", headers=None):
    token_headers = {"kid": key_id, "typ": "at+jwt"}
    token_headers.update(headers or {})
    return jwt.encode(
        payload or claims(),
        private_key,
        algorithm="RS256",
        headers=token_headers,
    )


def test_valid_oidc_token_maps_security_principal_and_uses_jwks_cache() -> None:
    private_key, jwk = key_material()
    fetcher = StubFetcher(
        {
            JWKS_URL: FetchResult(
                document={"keys": [jwk]},
                max_age_seconds=300,
            )
        }
    )
    verifier = OIDCVerifier(settings=oidc_settings(), fetcher=fetcher)
    token = signed_token(private_key)

    first = asyncio.run(verifier.verify(token))
    second = asyncio.run(verifier.verify(token))

    assert first == second
    assert first.principal.principal_id == "user-123"
    assert first.principal.tenant_id == "tenant-a"
    assert first.principal.roles == frozenset({"contributor"})
    assert first.principal.compartments == frozenset({"project-x"})
    assert first.principal.clearance is Classification.CONFIDENTIAL
    assert first.token_id == "token-123"
    assert fetcher.calls == [JWKS_URL]


def test_identity_provider_internal_roles_are_not_application_roles() -> None:
    private_key, jwk = key_material()
    fetcher = StubFetcher({JWKS_URL: FetchResult({"keys": [jwk]}, 300)})
    verifier = OIDCVerifier(settings=oidc_settings(), fetcher=fetcher)

    mixed = signed_token(
        private_key,
        claims(roles=["contributor", "offline_access", "uma_authorization"]),
    )
    verified = asyncio.run(verifier.verify(mixed))
    assert verified.principal.roles == frozenset({"contributor"})

    unknown_only = signed_token(private_key, claims(roles=["offline_access"]))
    with pytest.raises(AuthenticationError, match="invalid_token"):
        asyncio.run(verifier.verify(unknown_only))


@pytest.mark.parametrize(
    "payload_change",
    [
        {"aud": "different-api"},
        {"iss": "https://attacker.example"},
        {"exp": 1},
        {"tenant_id": None},
        {"roles": "contributor"},
        {"clearance": "unknown"},
    ],
)
def test_invalid_token_claims_fail_closed(payload_change) -> None:
    private_key, jwk = key_material()
    fetcher = StubFetcher({JWKS_URL: FetchResult({"keys": [jwk]}, 300)})
    verifier = OIDCVerifier(settings=oidc_settings(), fetcher=fetcher)
    token = signed_token(private_key, claims(**payload_change))

    with pytest.raises(AuthenticationError, match="invalid_token"):
        asyncio.run(verifier.verify(token))


def test_disallowed_algorithm_is_rejected_before_jwks_fetch() -> None:
    fetcher = StubFetcher({JWKS_URL: FetchResult({"keys": []}, 300)})
    verifier = OIDCVerifier(settings=oidc_settings(), fetcher=fetcher)
    token = jwt.encode(
        claims(),
        "not-a-trusted-key-with-32-bytes!",
        algorithm="HS256",
        headers={"kid": "key-1"},
    )

    with pytest.raises(AuthenticationError, match="invalid_token"):
        asyncio.run(verifier.verify(token))
    assert fetcher.calls == []


def test_unknown_kid_forces_bounded_jwks_rotation_refresh() -> None:
    first_private, first_jwk = key_material("key-1")
    second_private, second_jwk = key_material("key-2")
    fetcher = StubFetcher(
        {
            JWKS_URL: [
                FetchResult({"keys": [first_jwk]}, 300),
                FetchResult({"keys": [first_jwk, second_jwk]}, 300),
            ]
        }
    )
    verifier = OIDCVerifier(settings=oidc_settings(), fetcher=fetcher)

    asyncio.run(verifier.verify(signed_token(first_private, key_id="key-1")))
    rotated = asyncio.run(verifier.verify(signed_token(second_private, key_id="key-2")))

    assert rotated.principal.tenant_id == "tenant-a"
    assert fetcher.calls == [JWKS_URL, JWKS_URL]


def test_expired_jwks_cache_fails_closed_when_provider_is_unavailable() -> None:
    private_key, jwk = key_material()
    current_time = [100.0]
    fetcher = StubFetcher(
        {
            JWKS_URL: [
                FetchResult({"keys": [jwk]}, 30),
                IdentityProviderUnavailable("provider unavailable"),
            ]
        }
    )
    verifier = OIDCVerifier(
        settings=oidc_settings(),
        fetcher=fetcher,
        clock=lambda: current_time[0],
    )
    token = signed_token(private_key)
    asyncio.run(verifier.verify(token))
    current_time[0] = 131.0

    with pytest.raises(IdentityProviderUnavailable):
        asyncio.run(verifier.verify(token))


def test_discovery_issuer_mismatch_is_rejected() -> None:
    discovery_url = f"{ISSUER}/.well-known/openid-configuration"
    settings = oidc_settings(COIFESP_OIDC_JWKS_URL="")
    fetcher = StubFetcher(
        {
            discovery_url: FetchResult(
                {
                    "issuer": "https://different-issuer.example",
                    "jwks_uri": JWKS_URL,
                },
                300,
            )
        }
    )
    verifier = OIDCVerifier(settings=settings, fetcher=fetcher)
    private_key, _ = key_material()

    with pytest.raises(
        IdentityProviderUnavailable,
        match="does not match",
    ):
        asyncio.run(verifier.verify(signed_token(private_key)))


def test_token_lifetime_and_multiple_audience_azp_are_bounded() -> None:
    private_key, jwk = key_material()
    fetcher = StubFetcher({JWKS_URL: FetchResult({"keys": [jwk]}, 300)})
    verifier = OIDCVerifier(settings=oidc_settings(), fetcher=fetcher)
    now = int(datetime.now(UTC).timestamp())
    long_lived = signed_token(
        private_key,
        claims(iat=now, exp=now + 7200),
    )
    ambiguous_audience = signed_token(
        private_key,
        claims(aud=[AUDIENCE, "another-api"], azp=None),
    )
    valid_keycloak_ui = signed_token(
        private_key,
        claims(aud=[AUDIENCE, "account"], azp="coifesp-local-ui"),
    )
    unauthorized_client = signed_token(
        private_key,
        claims(aud=AUDIENCE, azp="unreviewed-client"),
    )

    with pytest.raises(AuthenticationError):
        asyncio.run(verifier.verify(long_lived))
    with pytest.raises(AuthenticationError):
        asyncio.run(verifier.verify(ambiguous_audience))
    assert asyncio.run(verifier.verify(valid_keycloak_ui)).audience == AUDIENCE
    with pytest.raises(AuthenticationError):
        asyncio.run(verifier.verify(unauthorized_client))


def test_symmetric_oidc_algorithm_configuration_is_rejected() -> None:
    settings = oidc_settings(COIFESP_OIDC_ALGORITHMS="HS256")
    with pytest.raises(
        ConfigurationError,
        match="approved asymmetric algorithms",
    ):
        settings.validate(require_auth=True)
