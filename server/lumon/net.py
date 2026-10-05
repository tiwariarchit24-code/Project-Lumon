"""
The ONLY place where the backend talks to the outside network.

Keeping every external request behind this module makes the air-gap rule
easy to enforce and easy to audit:
  - fetch_bytes() refuses to run in air-gapped mode (urllib requests)
  - require_connected() must be called before GDAL/rasterio reads remote
    imagery; gdal_network_options() configures those reads
No other module opens network connections.
"""

import hashlib
import json
import ssl
import urllib.request

import certifi  # trusted certificate bundle, installed in the project venv

from . import settings

# The python.org macOS build of Python does not use the system certificate
# store, so HTTPS fails unless we give it a certificate bundle. Using
# certifi's bundle keeps this fix inside the project (no system changes).
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

# Identify ourselves politely to public services.
USER_AGENT = "ProjectLumon/0.1 (local geospatial research workstation)"


class OfflineError(Exception):
    """Raised when code tries to use the network while in air-gapped mode."""


def fetch_bytes(url: str, timeout: int = 60, headers: dict | None = None, json_body: dict | None = None) -> bytes:
    """
    Download `url` and return the raw response body.

    - json_body: when given, the request is a POST with this JSON payload
      (used for STAC catalogue searches); otherwise it is a GET.

    - Raises OfflineError in air-gapped mode (no request is attempted).
    - Raises the normal urllib errors on HTTP or network failure; callers
      catch these and record the failure in the source health table.
    """
    if settings.operating_mode() != "connected":
        raise OfflineError("Network access is disabled (LUMON_MODE=airgapped).")

    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    payload = None
    if json_body is not None:
        payload = json.dumps(json_body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=payload, headers=request_headers)
    with urllib.request.urlopen(request, timeout=timeout, context=SSL_CONTEXT) as response:
        return response.read()


def fetch_response(url: str, timeout: int = 20, headers: dict | None = None, form_body: dict | None = None) -> tuple[int, dict, bytes]:
    """
    Like fetch_bytes, but returns (HTTP status, response headers, body)
    instead of raising on HTTP errors, so callers can read rate-limit
    headers and react to 401/429 themselves.

    - form_body: when given, the request is a form-encoded POST (used for
      the OAuth2 token request).
    - Still raises OfflineError in air-gapped mode, and urllib errors for
      network failures (DNS, timeout, connection refused).
    """
    import urllib.error
    import urllib.parse

    if settings.operating_mode() != "connected":
        raise OfflineError("Network access is disabled (LUMON_MODE=airgapped).")
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    payload = None
    if form_body is not None:
        payload = urllib.parse.urlencode(form_body).encode("utf-8")
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=payload, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=SSL_CONTEXT) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:  # 4xx / 5xx: hand the details back
        return error.code, dict(error.headers or {}), error.read() or b""


def sha256_of_bytes(data: bytes) -> str:
    """Return the SHA-256 checksum of some bytes, used for provenance records."""
    return hashlib.sha256(data).hexdigest()


def require_connected(purpose: str) -> None:
    """Raise OfflineError unless we are in connected mode (used before GDAL network reads)."""
    if settings.operating_mode() != "connected":
        raise OfflineError(f"{purpose} needs the network, but LUMON_MODE=airgapped.")


def gdal_network_options() -> dict:
    """
    GDAL settings for reading Cloud-Optimized GeoTIFFs over HTTPS.

    GDAL (used by rasterio) has its own HTTP client, so it is the second -
    and only other - way the backend can reach the network. Callers must
    call require_connected() first; these options only make the reads
    efficient (no directory listing, only .tif files) and give GDAL the same
    certificate bundle as urllib.
    """
    return {
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
        "GDAL_HTTP_MAX_RETRY": "3",
        "GDAL_HTTP_RETRY_DELAY": "2",
        "CURL_CA_BUNDLE": certifi.where(),
        "GDAL_HTTP_USERAGENT": USER_AGENT,
    }
