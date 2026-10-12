"""SQLite storage via SQLAlchemy 2.0."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import DatabaseError
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
    access_token_enc: Mapped[str] = mapped_column(Text)  # AES-GCM ciphertext, never plaintext
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
    # Full Plaid payload as AES-GCM ciphertext (see sync.raw_context), kept for future
    # feature engineering / ML.
    raw_enc: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


def _sqlite_pragmas(dbapi_conn, _record):
    # Registered per engine in make_engine, *after* the dialect's own connect hook has applied
    # the SQLCipher key. A class-wide listener would run first and hit an unreadable database.
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.close()


def make_engine(db_path: Path | str, db_key: bytes) -> Engine:
    """Open (creating if needed) the SQLCipher database at `db_path`, or ":memory:".

    `db_key` is the raw 32-byte SQLCipher key. It is passed as a hex blob, so SQLCipher skips
    password stretching and the key never has to be quoted or escaped.
    """
    in_memory = str(db_path) == ":memory:"
    if not in_memory:
        # Transaction data is PII: owner-only from creation. SQLite's -wal/-shm files inherit
        # the main file's mode, so creating it first closes the window a later chmod would leave.
        create_private_file(Path(db_path))
    url = URL.create(
        "sqlite+pysqlcipher",
        password=f"x'{db_key.hex()}'",
        database=None if in_memory else str(db_path),
    )
    kwargs: dict = {"connect_args": {"check_same_thread": False}}
    if in_memory:
        from sqlalchemy.pool import StaticPool

        kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    event.listen(engine, "connect", _sqlite_pragmas)
    try:
        with engine.connect() as conn:  # surfaces a wrong key / unencrypted file right away
            conn.exec_driver_sql("SELECT count(*) FROM sqlite_master")
    except DatabaseError as exc:
        engine.dispose()
        raise RuntimeError(
            f"Could not unlock {db_path}: wrong master key, or the file is not a findata "
            "encrypted database (a database created before encryption was added must be "
            "re-created)."
        ) from exc
    Base.metadata.create_all(engine)
    return engine


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)
