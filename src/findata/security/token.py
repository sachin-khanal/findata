"""The local API token that gates the HTTP API.

Requiring it in a custom header also forces a CORS preflight, which blocks drive-by requests
from other websites.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from findata.security.files import load_or_create_secret


def load_or_create_api_token(path: Path) -> str:
    return load_or_create_secret(path, lambda: secrets.token_urlsafe(32).encode()).decode()
