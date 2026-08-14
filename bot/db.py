from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from bot.config import DEFAULT_CURRENCY, DEFAULT_REMIND_AT, DEFAULT_TZ, get_settings
from bot.models import Category, Transaction, User

DEFAULT_EXPENSE_CATEGORIES = [
    "food",
    "transport",
    "bills",
    "shopping",
    "health",
    "entertainment",
    "other",
]
DEFAULT_INCOME_CATEGORIES = ["salary", "freelance", "gift", "other"]

_engine = None
_SessionLocal = None


def normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def get_engine():
    global _engine
    if _engine is None:
        url = normalize_database_url(get_settings().database_url)
        connect_args = {}
        if url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
        _engine = create_engine(url, connect_args=connect_args, future=True)
    return _engine


def get_session_factory():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(),
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            future=True,
        )
    return _SessionLocal


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def seed_categories(session: Session) -> None:
    existing = {(row.name, row.kind) for row in session.scalars(select(Category))}
    for name in DEFAULT_EXPENSE_CATEGORIES:
        if (name, "expense") not in existing:
            session.add(Category(name=name, kind="expense", is_default=True))
    for name in DEFAULT_INCOME_CATEGORIES:
        if (name, "income") not in existing:
            session.add(Category(name=name, kind="income", is_default=True))
    session.flush()


def run_migrations() -> None:
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    command.upgrade(cfg, "head")


def init_db() -> None:
    run_migrations()
    with session_scope() as session:
        seed_categories(session)


def get_or_create_user(session: Session, telegram_id: int) -> User:
    user = session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        settings = get_settings()
        user = User(
            telegram_id=telegram_id,
            timezone=settings.tz or DEFAULT_TZ,
            remind_at=DEFAULT_REMIND_AT,
            currency=DEFAULT_CURRENCY,
        )
        session.add(user)
        session.flush()
    return user


def list_categories(session: Session, kind: str | None = None) -> list[Category]:
    stmt = select(Category).order_by(Category.kind, Category.name)
    if kind:
        stmt = stmt.where(Category.kind == kind)
    return list(session.scalars(stmt))


def find_category(session: Session, name: str, kind: str) -> Category | None:
    return session.scalar(
        select(Category).where(
            func.lower(Category.name) == name.strip().lower(),
            Category.kind == kind,
        )
    )


def add_category(session: Session, name: str, kind: str) -> Category:
    category = Category(name=name.strip().lower(), kind=kind, is_default=False)
    session.add(category)
    session.flush()
    return category


def category_usage_count(session: Session, category_id: int) -> int:
    return int(
        session.scalar(
            select(func.count()).where(Transaction.category_id == category_id)
        )
        or 0
    )


def add_transaction(
    session: Session,
    user_id: int,
    kind: str,
    amount_cents: int,
    category_id: int | None,
    note: str | None,
    occurred_on: date,
) -> Transaction:
    tx = Transaction(
        user_id=user_id,
        kind=kind,
        amount_cents=amount_cents,
        category_id=category_id,
        note=(note.strip() if note else None) or None,
        occurred_on=occurred_on,
    )
    session.add(tx)
    session.flush()
    return tx


def last_transaction(session: Session, user_id: int) -> Transaction | None:
    return session.scalar(
        select(Transaction)
        .where(Transaction.user_id == user_id)
        .order_by(Transaction.created_at.desc(), Transaction.id.desc())
        .limit(1)
    )
