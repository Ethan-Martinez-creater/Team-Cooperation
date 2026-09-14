"""Small single-tenant adapter for the existing connector protocol, not an OIDC server."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import jwt
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

PATHS = frozenset({"/v1/github/issues", "/v1/github/workflow-dispatches", "/v1/github/commit-checks"})
READ_PATH = "/v1/github/commit-checks"


@dataclass(frozen=True)
class GitHubAdapterSettings:
    client_id: str
    client_secret: str = field(repr=False)
    github_token: str = field(repr=False)
    repositories: frozenset[str]
    ledger_path: Path

    def __post_init__(self):
        if not self.client_id or len(self.client_secret) < 32 or not self.github_token:
            raise ValueError("adapter credentials are missing or invalid")
        if not self.repositories or not self.ledger_path.is_absolute():
            raise ValueError("repositories and absolute persistent ledger path are required")

    @classmethod
    def from_env(cls, environment=None):
        source = os.environ if environment is None else environment
        return cls(
            client_id=source["COIFESP_GITHUB_ADAPTER_CLIENT_ID"],
            client_secret=source["COIFESP_GITHUB_ADAPTER_CLIENT_SECRET"],
            github_token=source["COIFESP_GITHUB_TOKEN"],
            repositories=frozenset(json.loads(source["COIFESP_GITHUB_REPOSITORIES"])),
            ledger_path=Path(source["COIFESP_GITHUB_ADAPTER_LEDGER"]),
        )


class AdapterLedger:
    """Persist intent before sending a write. Unknown outcomes are never resent."""

    def __init__(self, path):
        self.path = path
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS operations ("
                       "key TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def begin(self, key, digest):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT digest,result FROM operations WHERE key=?", (key,)).fetchone()
            if row:
                if row[0] != digest:
                    raise HTTPException(409, "idempotency_payload_conflict")
                if row[1] is None:
                    raise HTTPException(409, "write_outcome_unknown_do_not_resend")
                result = json.loads(row[1])
                if not isinstance(result, dict):
                    raise HTTPException(409, "write_outcome_unknown_do_not_resend")
                return result
            db.execute("INSERT INTO operations(key,digest) VALUES (?,?)", (key, digest))
        return None

    def complete(self, key, result):
        with self.connect() as db:
            db.execute("UPDATE operations SET result=? WHERE key=? AND result IS NULL",
                       (json.dumps(result, sort_keys=True), key))


async def bounded_body(request, maximum):
    value = bytearray()
    async for chunk in request.stream():
        value.extend(chunk)
        if len(value) > maximum:
            raise HTTPException(413, "request_too_large")
    return bytes(value)


def create_app(settings: GitHubAdapterSettings, *, native_client=None):
    # Late import allows independent ledger/auth unit tests without outbound HTTP.
    if native_client is None:
        from .github_native import NativeGitHubClient
        native_client = NativeGitHubClient(token=settings.github_token, repositories=settings.repositories)
    ledger = AdapterLedger(settings.ledger_path)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/oauth/token")
    async def token(request: Request):
        try:
            form = parse_qs((await bounded_body(request, 8192)).decode("utf-8"), strict_parsing=True)
        except (UnicodeError, ValueError):
            raise HTTPException(400, "invalid_request") from None
        if any(len(v) != 1 for v in form.values()):
            raise HTTPException(400, "invalid_request")
        value = {k: v[0] for k, v in form.items()}
        if (value.get("grant_type") != "client_credentials"
                or not hmac.compare_digest(value.get("client_id", "").encode(), settings.client_id.encode())
                or not hmac.compare_digest(value.get("client_secret", "").encode(), settings.client_secret.encode())):
            raise HTTPException(401, "invalid_client")
        if value.get("scope") != "github.adapter":
            raise HTTPException(400, "invalid_scope")
        now = int(time.time())
        encoded = jwt.encode({"sub": settings.client_id, "aud": "github-adapter", "iat": now,
                              "exp": now + 300, "scope": "github.adapter"},
                             settings.client_secret, algorithm="HS256")
        return JSONResponse({"access_token": encoded, "token_type": "Bearer", "expires_in": 300},
                            headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    @app.post("/v1/github/{operation}")
    async def execute(operation: str, request: Request):
        path = "/v1/github/" + operation
        if path not in PATHS:
            raise HTTPException(404, "unknown_operation")
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "invalid_token")
        try:
            claims = jwt.decode(authorization[7:], settings.client_secret, algorithms=["HS256"],
                                audience="github-adapter", options={"require": ["exp", "sub", "iat"]})
            if claims["sub"] != settings.client_id or claims.get("scope") != "github.adapter":
                raise ValueError()
        except (jwt.PyJWTError, ValueError):
            raise HTTPException(401, "invalid_token") from None
        key = request.headers.get("idempotency-key", "")
        if not key or len(key) > 128:
            raise HTTPException(400, "invalid_idempotency_key")
        try:
            body = json.loads(await bounded_body(request, 1_048_576))
            if not isinstance(body, dict) or body.get("repository") not in settings.repositories:
                raise ValueError()
            canonical = json.dumps([path, body], sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (ValueError, TypeError, RecursionError):
            raise HTTPException(400, "invalid_request") from None
        # Store only a keyed request digest and minimal result, never issue text or inputs.
        digest = hmac.new(settings.client_secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
        ledger_key = hashlib.sha256((settings.client_id + ":" + key).encode()).hexdigest()
        if path != READ_PATH:
            previous = ledger.begin(ledger_key, digest)
            if previous is not None:
                return previous
        try:
            result = await native_client.execute(path, body)
        except (RuntimeError, ValueError, httpx.HTTPError):
            # An upstream error may follow a successful remote write. Keep durable intent;
            # retrying this key returns 409, not a second issue/workflow dispatch.
            raise HTTPException(502, "github_operation_failed_or_outcome_unknown") from None
        if not isinstance(result, dict) or result.get("repository") != body["repository"]:
            raise HTTPException(502, "github_invalid_response")
        if path != READ_PATH:
            ledger.complete(ledger_key, result)
        return result

    return app


def main():
    import uvicorn
    settings = GitHubAdapterSettings.from_env()
    # Bind locally; publish through an HTTPS reverse proxy for SecureConnectorClient.
    uvicorn.run(create_app(settings), host="127.0.0.1", port=8011, access_log=False)


if __name__ == "__main__":
    main()
