"""Re-export of the API-token loader, which now lives in `findata.privfs`."""

from findata.privfs import load_or_create_api_token

__all__ = ["load_or_create_api_token"]
