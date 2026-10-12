"""SQLite storage via SQLAlchemy 2.0."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from sqlalchemy.types import TypeDecorator

from findata.security import create_private_file


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class DecimalString(TypeDecorator):
    """Store money as exact decimal strings (SQLite has no native DECIMAL)."""

    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else str(Decimal(str(value)))

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


class Base(DeclarativeBase):
    pass


class Item(Base):
    """A Plaid Item: one login at one institution."""

    __tablename__ = "items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # Plaid item_id
    institution_id: Mapped[str | None] = mapped_column(String(64))
    institution_name: Mapped[str | None] = mapped_column(String(255))
    access_token_enc: Mapped[str] = mapped_column(Text)  # Fernet ciphertext, never plaintext
    sync_cursor: Mapped[str | None] = mapped_column(Text)
    last_synced_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    accounts: Mapped[list[Account]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # Plaid account_id
    item_id: Mapped[str] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    official_name: Mapped[str | None] = mapped_column(String(255))
    mask: Mapped[str | None] = mapped_column(String(8))
    type: Mapped[str | None] = mapped_column(String(32))
    subtype: Mapped[str | None] = mapped_column(String(64))
    current_balance: Mapped[Decimal | None] = mapped_column(DecimalString)
    available_balance: Mapped[Decimal | None] = mapped_column(DecimalString)
    iso_currency_code: Mapped[str | None] = mapped_column(String(8))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    item: Mapped[Item] = relationship(back_populates="accounts")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # Plaid transaction_id
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    # Plaid convention: positive = money out of the account, negative = money in.
    amount: Mapped[Decimal] = mapped_column(DecimalString)
    iso_currency_code: Mapped[str | None] = mapped_column(String(8))
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    authorized_date: Mapped[dt.date | None] = mapped_column(Date)
    name: Mapped[str] = mapped_column(String(512))
    merchant_name: Mapped[str | None] = mapped_column(String(255))
    pending: Mapped[bool] = mapped_column(Boolean, default=False)
    pending_transaction_id: Mapped[str | None] = mapped_column(String(64))
    payment_channel: Mapped[str | None] = mapped_column(String(32))
    category_primary: Mapped[str | None] = mapped_column(String(64), index=True)
    category_detailed: Mapped[str | None] = mapped_column(String(128))
    # Full Plaid payload, kept for future feature engineering / ML.
    raw: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.close()


def make_engine(db_path: Path | str) -> Engine:
    url = "sqlite://" if str(db_path) == ":memory:" else f"sqlite:///{db_path}"
    if url != "sqlite://":
        # Transaction data is PII: owner-only from creation. SQLite's -wal/-shm files inherit
        # the main file's mode, so creating it first closes the window a later chmod would leave.
        create_private_file(Path(db_path))
    kwargs = {"connect_args": {"check_same_thread": False}}
    if url == "sqlite://":
        from sqlalchemy.pool import StaticPool

        kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    Base.metadata.create_all(engine)
    return engine


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)
