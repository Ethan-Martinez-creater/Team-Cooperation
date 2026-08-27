from __future__ import annotations

import json
import re
import uuid
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, *, oidc_issuer: str | None = None) -> None:
        self.app = app
        parsed = urlsplit(oidc_issuer) if oidc_issuer else None
        self.oidc_origin = (
            f"{parsed.scheme}://{parsed.netloc}"
            if parsed and parsed.scheme in {"http", "https"} and parsed.netloc
            else None
        )

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", [])
        request_ids = [
            value.decode("latin-1") for name, value in headers if name.lower() == b"x-request-id"
        ]
        supplied = request_ids[0] if len(request_ids) == 1 else ""
        request_id = supplied if supplied and _REQUEST_ID.fullmatch(supplied) else str(uuid.uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        path = scope.get("path", "")
        # The OIDC silent-renewal page is intentionally frameable by the same
        # origin only (it runs inside a workspace iframe after prompt=none);
        # every other /app/ document keeps frame-ancestors 'none'.
        silent_callback = path == "/app/silent-callback.html"

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers", []))
                response_headers.extend(
                    [
                        (b"x-request-id", request_id.encode("ascii")),
                        (b"cache-control", b"no-store"),
                        (b"x-content-type-options", b"nosniff"),
                        (
                            b"x-frame-options",
                            b"SAMEORIGIN" if silent_callback else b"DENY",
                        ),
                        (b"referrer-policy", b"no-referrer"),
                        (
                            b"permissions-policy",
                            b"camera=(), microphone=(), geolocation=()",
                        ),
                        (
                            b"content-security-policy",
                            (
                                (
                                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                                    "connect-src 'self'"
                                    + (f" {self.oidc_origin}" if self.oidc_origin else "")
                                    + "; frame-src 'self'"
                                    + (f" {self.oidc_origin}" if self.oidc_origin else "")
                                    + "; img-src 'self'; base-uri 'none'; "
                                    "form-action 'self' https: http:; "
                                    "frame-ancestors "
                                    + ("'self'" if silent_callback else "'none'")
                                ).encode("ascii")
                                if path.startswith("/app/")
                                else b"default-src 'none'; frame-ancestors 'none'"
                            ),
                        ),
                    ]
                )
                message["headers"] = response_headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


class BoundedBodyMiddleware:
    """Buffers bounded control-plane JSON bodies before endpoint dispatch."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int,
        artifact_upload_bytes: int | None = None,
    ) -> None:
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.artifact_upload_bytes = artifact_upload_bytes

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        is_artifact_upload = path == "/v1/artifacts:upload"
        is_project_upload = path.startswith("/v1/projects/") and path.endswith("/resources:upload")
        if (is_artifact_upload or is_project_upload) and self.artifact_upload_bytes:
            content_length = _content_length(scope)
            if content_length is not None and content_length > self.artifact_upload_bytes + 65_536:
                await _send_too_large(scope, send)
                return
            await self.app(scope, receive, send)
            return
        content_length = _content_length(scope)
        if content_length is not None and content_length > self.max_body_bytes:
            await _send_too_large(scope, send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_body_bytes:
                await _send_too_large(scope, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {
                "type": "http.request",
                "body": bytes(body),
                "more_body": False,
            }

        await self.app(scope, replay, send)


def _content_length(scope: Scope) -> int | None:
    raw_values = [
        value for name, value in scope.get("headers", []) if name.lower() == b"content-length"
    ]
    if not raw_values:
        return None
    if len(raw_values) != 1:
        return 2**63 - 1
    try:
        value = int(raw_values[0])
    except ValueError:
        return 2**63 - 1
    return value if value >= 0 else 2**63 - 1


async def _send_too_large(scope: Scope, send: Send) -> None:
    request_id = scope.get("state", {}).get("request_id", "")
    body = json.dumps(
        {
            "type": "https://coifesp.dev/problems/request-too-large",
            "title": "Request body too large",
            "status": 413,
            "detail": "The request body exceeds the configured limit.",
            "request_id": request_id,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/problem+json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
