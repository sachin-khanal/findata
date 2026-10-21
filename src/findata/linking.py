"""Item lifecycle: link a bank connection, reuse the Sandbox one, or revoke and remove it."""

from __future__ import annotations

from sqlalchemy.orm import Session

from findata import queries
from findata.contexts import token_context
from findata.db import Item
from findata.plaid_gateway import SANDBOX_INSTITUTION_ID, PlaidError, PlaidGateway
from findata.security import FieldCipher


def link_item(
    session: Session, gateway: PlaidGateway, cipher: FieldCipher, public_token: str
) -> Item:
    """Exchange a public token and store the resulting Item with an encrypted access token."""
    item_id, access_token = gateway.exchange_public_token(public_token)
    meta = gateway.get_item(access_token)
    item = session.get(Item, item_id) or Item(id=item_id)
    item.access_token_enc = cipher.encrypt(access_token, token_context(item_id))
    item.institution_id = meta.get("institution_id")
    item.institution_name = meta.get("institution_name")
    session.add(item)
    session.commit()
    return item


def find_sandbox_item(session: Session) -> Item | None:
    """The already-linked Plaid Sandbox bank, if there is one."""
    return queries.find_item_by_institution(session, SANDBOX_INSTITUTION_ID)


def link_sandbox_item(
    session: Session, gateway: PlaidGateway, cipher: FieldCipher, username: str
) -> Item:
    """Create a Sandbox test connection (no browser) and store it."""
    return link_item(session, gateway, cipher, gateway.sandbox_public_token(username))


def remove_item(
    session: Session,
    gateway: PlaidGateway,
    cipher: FieldCipher,
    item: Item,
    *,
    local_only: bool = False,
) -> None:
    """Revoke `item` at Plaid, then delete it and its accounts/transactions locally.

    Plaid first: if that fails the local rows stay, so the token is never orphaned. An Item
    Plaid no longer knows (ITEM_NOT_FOUND) is already revoked, so local cleanup proceeds.
    `local_only` skips the Plaid call (e.g. Sandbox data that was reset on Plaid's side).
    """
    if not local_only:
        try:
            gateway.item_remove(cipher.decrypt(item.access_token_enc, token_context(item.id)))
        except PlaidError as exc:
            if exc.error_code != "ITEM_NOT_FOUND":
                raise
    session.delete(item)
    session.commit()
