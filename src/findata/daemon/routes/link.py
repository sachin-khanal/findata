"""Plaid Link: create a link token and exchange the resulting public token."""

from __future__ import annotations

from fastapi import APIRouter

from findata import queries
from findata.daemon.deps import PLAID_WRITE_LOCK, GatewayDep, SessionDep, StateDep, require
from findata.daemon.routes.data import item_out
from findata.linking import link_item
from findata.wire import ExchangeIn, ItemOut

LOCAL_CLIENT_USER_ID = "findata-local-user"

router = APIRouter(prefix="/v1/link", dependencies=[require("link")])


@router.post("/token", name="link.token")
def link_token(gateway: GatewayDep) -> dict[str, str]:
    return {"link_token": gateway.create_link_token(LOCAL_CLIENT_USER_ID)}


@router.post("/exchange", name="link.exchange")
def exchange(
    body: ExchangeIn, session: SessionDep, gateway: GatewayDep, state: StateDep
) -> ItemOut:
    with PLAID_WRITE_LOCK:
        item = link_item(session, gateway, state.runtime.cipher, body.public_token)
    state.events.publish("item_linked", item_id=item.id, institution=item.institution_name)
    return item_out(next(s for s in queries.list_item_summaries(session) if s.item.id == item.id))
