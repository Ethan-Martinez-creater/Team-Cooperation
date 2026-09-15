from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Depends
from starlette.requests import HTTPConnection

from ..auth import OIDCVerifier, VerifiedIdentity
from ..errors import AuthenticationError, PolicyDenied


@dataclass(frozen=True, slots=True)
class Authenticated:
    identity: VerifiedIdentity

    @property
    def principal(self):
        return self.identity.principal


class BearerAuthenticator:
    def __init__(
        self,
        verifier: OIDCVerifier,
        *,
        identity_sync: Callable[[VerifiedIdentity], object] | None = None,
    ) -> None:
        self.verifier = verifier
        self.identity_sync = identity_sync

    async def __call__(self, request: HTTPConnection) -> Authenticated:
        values = request.headers.getlist("authorization")
        if len(values) != 1:
            raise AuthenticationError("invalid_token")
        scheme, separator, token = values[0].partition(" ")
        if (
            not separator
            or scheme.lower() != "bearer"
            or not token
            or token.strip() != token
            or " " in token
        ):
            raise AuthenticationError("invalid_token")
        identity = await self.verifier.verify(token)
        if self.identity_sync is not None:
            result = self.identity_sync(identity)
            if inspect.isawaitable(result):
                await result
        request.state.identity = identity
        return Authenticated(identity=identity)


def require_roles(
    authenticator: BearerAuthenticator,
    *required_roles: str,
) -> Callable:
    if not required_roles or any(not role for role in required_roles):
        raise ValueError("at least one non-empty role is required")

    async def authorize(
        authenticated: Authenticated = Depends(authenticator),
    ) -> Authenticated:
        if not set(required_roles).issubset(authenticated.principal.roles):
            raise PolicyDenied("required control-plane role is missing")
        return authenticated

    return authorize
