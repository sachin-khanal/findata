"""Local HTTP API (FastAPI). Binds to 127.0.0.1 only; every /api route needs the local token."""

import datetime as dt
import secrets
from collections.abc import Iterator
from decimal import Decimal
from importlib.resources import files
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware

from findata.config import Settings, get_settings
from findata.db import Account, Item, Transaction, make_engine, make_session_factory
from findata.plaid_gateway import PlaidClient, PlaidError, PlaidGateway
from findata.security import TokenCipher, load_or_create_api_token
from findata.sync import link_item, sync_all

TOKEN_HEADER = "X-Findata-Token"  # noqa: S105 (header name, not a secret)
LOCAL_CLIENT_USER_ID = "findata-local-user"


# ---------- response models (also the future agent-facing contract) ----------


class ItemOut(BaseModel):
    id: str
    institution_name: str | None
    last_synced_at: dt.datetime | None


class AccountOut(BaseModel):
    id: str
    item_id: str
    name: str
    mask: str | None
    type: str | None
    subtype: str | None
    current_balance: Decimal | None
    available_balance: Decimal | None
    iso_currency_code: str | None


class TransactionOut(BaseModel):
    id: str
    account_id: str
    date: dt.date
    amount: Decimal
    iso_currency_code: str | None
    name: str
    merchant_name: str | None
    pending: bool
    category_primary: str | None
    category_detailed: str | None


class ExchangeIn(BaseModel):
    public_token: str


class SyncOut(BaseModel):
    item_id: str
    added: int
    modified: int
    removed: int
    status: str | None


# ---------- app factory ----------


def create_app(
    settings: Settings | None = None,
    gateway: PlaidGateway | None = None,
    engine: Engine | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    engine = engine or make_engine(settings.db_path)
    session_factory = make_session_factory(engine)
    cipher = TokenCipher.from_key_file(settings.key_path)
    api_token = load_or_create_api_token(settings.api_token_path)
    _gateway: list[PlaidGateway | None] = [gateway]

    app = FastAPI(title="findata", version="0.1.0", docs_url="/docs", redoc_url=None)
    # Blocks DNS-rebinding attacks against the localhost server.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    def get_session() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    def get_gateway() -> PlaidGateway:
        if _gateway[0] is None:  # lazy, so the app starts even before keys are configured
            try:
                _gateway[0] = PlaidClient(settings)
            except RuntimeError as exc:
                raise HTTPException(503, str(exc)) from exc
        return _gateway[0]

    def require_token(
        token: Annotated[str | None, Header(alias=TOKEN_HEADER)] = None,
    ) -> None:
        if not token or not secrets.compare_digest(token, api_token):
            raise HTTPException(401, f"Missing or invalid {TOKEN_HEADER} header")

    SessionDep = Annotated[Session, Depends(get_session)]
    GatewayDep = Annotated[PlaidGateway, Depends(get_gateway)]
    auth = [Depends(require_token)]

    @app.exception_handler(PlaidError)
    async def _plaid_error(_: Request, exc: PlaidError):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=502, content={"error_code": exc.error_code, "detail": str(exc)}
        )

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> str:
        html = files("findata.web").joinpath("index.html").read_text()
        # Same-origin page only; other origins can't read this response (no CORS).
        return html.replace("__FINDATA_TOKEN__", api_token).replace(
            "__PLAID_ENV__", settings.plaid_env
        )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "plaid_env": settings.plaid_env}

    @app.post("/api/link/token", dependencies=auth)
    def link_token(gw: GatewayDep) -> dict[str, str]:
        return {"link_token": gw.create_link_token(LOCAL_CLIENT_USER_ID)}

    @app.post("/api/link/exchange", dependencies=auth)
    def exchange(body: ExchangeIn, session: SessionDep, gw: GatewayDep) -> ItemOut:
        item = link_item(session, gw, cipher, body.public_token)
        return ItemOut.model_validate(item, from_attributes=True)

    @app.post("/api/sync", dependencies=auth)
    def sync(session: SessionDep, gw: GatewayDep) -> list[SyncOut]:
        return [SyncOut(**r.__dict__) for r in sync_all(session, gw, cipher)]

    @app.get("/api/items", dependencies=auth)
    def items(session: SessionDep) -> list[ItemOut]:
        rows = session.scalars(select(Item).order_by(Item.created_at)).all()
        return [ItemOut.model_validate(r, from_attributes=True) for r in rows]

    @app.get("/api/accounts", dependencies=auth)
    def accounts(session: SessionDep) -> list[AccountOut]:
        rows = session.scalars(select(Account).order_by(Account.name)).all()
        return [AccountOut.model_validate(r, from_attributes=True) for r in rows]

    @app.get("/api/transactions", dependencies=auth)
    def transactions(
        session: SessionDep,
        start: dt.date | None = None,
        end: dt.date | None = None,
        account_id: str | None = None,
        category: str | None = Query(None, description="personal_finance_category.primary"),
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> list[TransactionOut]:
        q = select(Transaction)
        if start:
            q = q.where(Transaction.date >= start)
        if end:
            q = q.where(Transaction.date <= end)
        if account_id:
            q = q.where(Transaction.account_id == account_id)
        if category:
            q = q.where(Transaction.category_primary == category)
        q = q.order_by(Transaction.date.desc(), Transaction.id).limit(limit).offset(offset)
        return [TransactionOut.model_validate(r, from_attributes=True) for r in session.scalars(q)]

    return app
