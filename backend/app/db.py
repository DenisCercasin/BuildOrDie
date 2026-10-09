from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, UniqueConstraint, create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


def uid():
    return str(uuid4())


def now():
    return datetime.now(timezone.utc).isoformat()


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    telegram_id: Mapped[str] = mapped_column(String(40), unique=True)
    display_name: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    enrollment_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class BookRequest(Base):
    __tablename__ = "book_requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    isbn: Mapped[str | None] = mapped_column(String(20), nullable=True)
    edition: Mapped[str | None] = mapped_column(String(100), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    budget_minor: Mapped[int] = mapped_column(Integer)
    min_savings_minor: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="SGD")
    latest_delivery_at: Mapped[str] = mapped_column(String(40))
    pickup_location: Mapped[str] = mapped_column(String(200))
    group_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(30), default="OPEN")
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class Offer(Base):
    __tablename__ = "offers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    request_id: Mapped[str] = mapped_column(ForeignKey("book_requests.id"), index=True)
    merchant: Mapped[str] = mapped_column(String(100))
    variant_id: Mapped[str] = mapped_column(String(200))
    unit_price_minor: Mapped[int] = mapped_column(Integer)
    individual_total_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="SGD")
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    delivery_at: Mapped[str] = mapped_column(String(40))
    expires_at: Mapped[str] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(30), default="mock")
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class Group(Base):
    __tablename__ = "groups"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    client_reference: Mapped[str] = mapped_column(String(100), unique=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    merchant: Mapped[str] = mapped_column(String(100))
    purchaser_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(40), default="PROPOSED")
    currency: Mapped[str] = mapped_column(String(3), default="SGD")
    total_minor: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[str] = mapped_column(String(40))
    delivery_at: Mapped[str] = mapped_column(String(40))
    pickup_location: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(String(1000))
    quote_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    shipping_address: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class Participant(Base):
    __tablename__ = "participants"
    __table_args__ = (UniqueConstraint("group_id", "request_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    group_id: Mapped[str] = mapped_column(ForeignKey("groups.id"), index=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("book_requests.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    offer_id: Mapped[str] = mapped_column(ForeignKey("offers.id"))
    amount_minor: Mapped[int] = mapped_column(Integer)
    approved_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payment_state: Mapped[str] = mapped_column(String(30), default="PENDING")


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    group_id: Mapped[str] = mapped_column(ForeignKey("groups.id"), unique=True)
    provider: Mapped[str] = mapped_column(String(30))
    state: Mapped[str] = mapped_column(String(40), default="CHECKOUT_PENDING")
    total_minor: Mapped[int] = mapped_column(Integer)
    final_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_checkout_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    merchant_order_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    approval_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Persist payload before contacting provider; same idempotency key survives restarts.
    checkout_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class PaymentTransaction(Base):
    __tablename__ = "payment_transactions"
    __table_args__ = (UniqueConstraint("group_id", "version", "request_id", "kind"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    group_id: Mapped[str] = mapped_column(ForeignKey("groups.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    request_id: Mapped[str] = mapped_column(ForeignKey("book_requests.id"))
    version: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(40))
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="SGD")
    simulated: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class Database:
    def __init__(self, url):
        if url.startswith("sqlite:///."):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        args = {"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, connect_args=args, pool_pre_ping=True)
        if self.engine.dialect.name == "sqlite":
            @event.listens_for(self.engine, "connect")
            def sqlite_settings(conn, _):
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA journal_mode=WAL")
        Base.metadata.create_all(self.engine)

    @contextmanager
    def transaction(self, write=False):
        with Session(self.engine, expire_on_commit=False) as session:
            if write and self.engine.dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
