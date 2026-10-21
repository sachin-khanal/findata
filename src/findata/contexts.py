"""Associated-data labels that bind a ciphertext to the row it belongs to.

Shared by the code that writes encrypted fields (linking, sync) so they cannot drift apart.
"""

from __future__ import annotations


def token_context(item_id: str) -> str:
    """Associated data binding an access-token ciphertext to its Item."""
    return f"items.access_token:{item_id}"


def raw_context(transaction_id: str) -> str:
    """Associated data binding a raw-payload ciphertext to its transaction."""
    return f"transactions.raw:{transaction_id}"
