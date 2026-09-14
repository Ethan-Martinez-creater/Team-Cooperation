from __future__ import annotations

import ssl


def explicit_ca_context(ca_bundle: str | None) -> ssl.SSLContext | None:
    """Build an explicit trust store without enabling proxy/environment trust."""

    if ca_bundle is None:
        return None
    return ssl.create_default_context(cafile=ca_bundle)
