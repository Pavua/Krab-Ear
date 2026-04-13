"""API versioning utilities for Krab Ear REST server.

Provides version detection from URL prefix, Accept header, or query param,
response header injection, deprecation signalling, and version metadata.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from flask import request, Response

from KrabEar.__version__ import __version__ as APP_VERSION


class APIVersion(Enum):
    V1 = "v1"
    V2 = "v2"


# The version served when no explicit version is requested.
DEFAULT_VERSION = APIVersion.V1

# Versions that are deprecated, mapped to their sunset ISO-8601 date.
DEPRECATED_VERSIONS: dict[APIVersion, str] = {}

# All versions the server understands.
SUPPORTED_VERSIONS = [APIVersion.V1, APIVersion.V2]


def get_api_version(req=None) -> APIVersion:
    """Detect the requested API version from the incoming Flask request.

    Detection order:
    1. URL path prefix — ``/v1/...`` or ``/v2/...``
    2. ``Accept`` header — ``application/vnd.krabear.v1+json``
    3. ``api_version`` query parameter — ``?api_version=v1``
    4. Falls back to :data:`DEFAULT_VERSION`.

    Args:
        req: Flask ``request`` object.  When ``None`` the global ``request``
             proxy is used (normal call from within a request context).

    Returns:
        The resolved :class:`APIVersion`.
    """
    if req is None:
        req = request

    # 1. URL prefix (/v1/ or /v2/)
    path = req.path or ""
    for version in SUPPORTED_VERSIONS:
        prefix = f"/{version.value}/"
        if path.startswith(prefix) or path == f"/{version.value}":
            return version

    # 2. Accept header: application/vnd.krabear.v1+json
    accept = req.headers.get("Accept", "")
    for version in SUPPORTED_VERSIONS:
        if f"vnd.krabear.{version.value}" in accept:
            return version

    # 3. Query parameter: ?api_version=v1
    qp = req.args.get("api_version", "").strip().lower()
    for version in SUPPORTED_VERSIONS:
        if qp == version.value:
            return version

    return DEFAULT_VERSION


def api_version_header():
    """Flask ``after_request`` handler that adds ``X-API-Version`` to every response.

    Register with::

        app.after_request(api_version_header)

    The header value reflects whichever version was resolved for the request.
    """
    def handler(response: Response) -> Response:
        try:
            version = get_api_version()
            response.headers["X-API-Version"] = version.value
        except RuntimeError:
            # Outside of a request context (e.g. tests that call directly).
            response.headers["X-API-Version"] = DEFAULT_VERSION.value
        return response

    return handler


def deprecation_warning(response: Response, version: str, sunset_date: str) -> Response:
    """Add ``Sunset`` and ``Deprecation`` headers to *response*.

    Args:
        response:     The Flask :class:`~flask.Response` to annotate.
        version:      The version string being deprecated, e.g. ``"v1"``.
        sunset_date:  ISO-8601 date after which the version will be removed,
                      e.g. ``"2027-01-01"``.

    Returns:
        The same response object with headers added.
    """
    response.headers["Deprecation"] = f'version="{version}"'
    response.headers["Sunset"] = sunset_date
    return response


def get_api_info() -> dict:
    """Return a structured summary of API version metadata.

    Returns:
        A dict with keys:
        - ``app_version``: Krab Ear application version string
        - ``current_version``: default API version string
        - ``supported_versions``: list of all supported version strings
        - ``deprecated_versions``: list of dicts with ``version`` and ``sunset_date``
    """
    return {
        "app_version": APP_VERSION,
        "current_version": DEFAULT_VERSION.value,
        "supported_versions": [v.value for v in SUPPORTED_VERSIONS],
        "deprecated_versions": [
            {"version": v.value, "sunset_date": sunset}
            for v, sunset in DEPRECATED_VERSIONS.items()
        ],
    }
