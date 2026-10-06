"""Jotted's own HTTPS downloads (rmapi, the self-update check), with certificates that work everywhere.

A frozen Python (the standalone build) looks for CA certificates where it was built, which
isn't there on the person's Mac, so plain `urllib` fails to verify anything. These downloads
trust what the operating system trusts instead, through `truststore`: that includes a
certificate an employer installs for a proxy that inspects HTTPS. The model SDKs bring their
own TLS and are left alone (no `truststore.inject_into_ssl()`).

Local calls to this machine's `jotted serve` are plain HTTP and don't come through here.
"""

from __future__ import annotations

import os
import ssl
import urllib.request
from urllib.parse import urlsplit

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


def ssl_context() -> ssl.SSLContext:
    """SSL_CERT_FILE / SSL_CERT_DIR when set (someone who sets them means it); else the OS trust
    store (truststore); else certifi's bundle; else Python's default."""
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return ssl.create_default_context()  # reads both
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # noqa: BLE001 - no usable OS store (an import error, an unsupported system)
        pass
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def urlopen(url_or_request: str | urllib.request.Request, *, timeout: float):
    """urllib.request.urlopen with ssl_context(); https only for anything not on this machine."""
    url = url_or_request.full_url if isinstance(url_or_request, urllib.request.Request) else url_or_request
    parts = urlsplit(url)
    if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in LOCAL_HOSTS):
        raise ValueError(f"refusing to fetch {url}: only https leaves this machine")
    return urllib.request.urlopen(url_or_request, timeout=timeout, context=ssl_context())  # noqa: S310 - checked above


def untrusted(error: BaseException) -> bool:
    """True when a download failed because the server's certificate wasn't trusted."""
    reason = getattr(error, "reason", error)  # urllib wraps it in a URLError
    return isinstance(reason, ssl.SSLCertVerificationError) or isinstance(error, ssl.SSLCertVerificationError)
