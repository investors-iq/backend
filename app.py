"""Read-only FastAPI service for Invest IQ's PostgreSQL data."""

from __future__ import annotations

import hmac
import logging
import os
from collections.abc import Generator
from datetime import datetime
from decimal import Decimal
from typing import Any, Generic, Literal, TypeVar

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.security import APIKeyHeader
from mangum import Mangum
from pydantic import BaseModel
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

Item = TypeVar("Item")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


class Page(BaseModel, Generic[Item]):
    items: list[Item]
    total: int
    limit: int
    offset: int


class Instrument(BaseModel):
    id: int
    exchange: str
    symbol: str
    name: str | None
    market_cap_cr: Decimal | None
    cap_category: str | None
    transaction_count: int


class Transaction(BaseModel):
    id: int
    instrument_id: int
    exchange: str
    symbol: str
    name: str | None
    type: Literal["BUY", "SELL"]
    quantity: Decimal
    price: Decimal
    price_derived: bool
    price_currency: str | None
    invested_amount: Decimal | None
    fees: Decimal
    trade_date: datetime | None
    notes: str | None
    sheet_snapshot: dict[str, Any] | None
    source: str
    created_at: datetime


class Holding(BaseModel):
    instrument_id: int
    exchange: str
    symbol: str
    name: str | None
    price_currency: str
    net_quantity: Decimal
    buy_quantity: Decimal
    sell_quantity: Decimal
    average_buy_price: Decimal | None
    average_sell_price: Decimal | None
    buy_invested_amount_recorded: Decimal
    sell_invested_amount_recorded: Decimal


class PortfolioSummary(BaseModel):
    transaction_count: int
    buy_transaction_count: int
    sell_transaction_count: int
    buy_invested_amount_recorded: Decimal
    sell_invested_amount_recorded: Decimal
    invested_amount_currency_note: str


app = FastAPI(
    title="Invest IQ API",
    description=(
        "Read-only access to instruments, transactions, and portfolio summaries "
        "stored in the Invest IQ PostgreSQL database."
    ),
    version="1.0.0",
)


def require_api_key(
    api_key: str | None = Depends(api_key_header),
) -> None:
    expected = os.getenv("API_KEY")
    if not expected or not expected.isascii():
        logger.error("API_KEY is not configured")
        raise HTTPException(status_code=503, detail="API authentication is not configured")
    if api_key is None or not api_key.isascii() or not hmac.compare_digest(api_key, expected):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )


def get_connection() -> Generator[psycopg.Connection[dict[str, Any]], None, None]:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL is not configured")
        raise HTTPException(status_code=503, detail="Database is not configured")

    connection: psycopg.Connection[dict[str, Any]] | None = None
    try:
        connection = psycopg.connect(
            database_url,
            connect_timeout=5,
            sslmode=os.getenv("DB_SSLMODE", "require"),
            row_factory=dict_row,
        )
        connection.execute("SET TRANSACTION READ ONLY")
        yield connection
    except psycopg.Error as exc:
        logger.exception("PostgreSQL request failed")
        raise HTTPException(status_code=503, detail="Database request failed") from exc
    finally:
        if connection is not None:
            connection.close()


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    """Liveness check; does not connect to the database."""
    return {"status": "ok"}


@app.get(
    "/api/v1/instruments",
    response_model=Page[Instrument],
    dependencies=[Depends(require_api_key)],
    tags=["instruments"],
)
def list_instruments(
    search: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    conn: psycopg.Connection[dict[str, Any]] = Depends(get_connection),
) -> Page[Instrument]:
    search_pattern = f"%{search}%" if search else None
    where = """
        (%s::text IS NULL OR i.symbol ILIKE %s OR COALESCE(i.name, '') ILIKE %s)
    """
    params = (search_pattern, search_pattern, search_pattern)
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS total FROM instruments i WHERE {where}", params)
        total = cur.fetchone()["total"]
        cur.execute(
            f"""
            SELECT i.id, i.exchange, i.symbol, i.name, i.market_cap_cr, i.cap_category,
                   COUNT(t.id)::integer AS transaction_count
            FROM instruments i
            LEFT JOIN transactions t
              ON t.instrument_id = i.id AND t.deleted_at IS NULL
            WHERE {where}
            GROUP BY i.id
            ORDER BY i.exchange, i.symbol, i.id
            LIMIT %s OFFSET %s
            """,
            (*params, limit, offset),
        )
        items = cur.fetchall()
    return Page[Instrument](items=items, total=total, limit=limit, offset=offset)


@app.get(
    "/api/v1/instruments/{instrument_id}",
    response_model=Instrument,
    dependencies=[Depends(require_api_key)],
    tags=["instruments"],
)
def get_instrument(
    instrument_id: int,
    conn: psycopg.Connection[dict[str, Any]] = Depends(get_connection),
) -> Instrument:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT i.id, i.exchange, i.symbol, i.name, i.market_cap_cr, i.cap_category,
                   COUNT(t.id)::integer AS transaction_count
            FROM instruments i
            LEFT JOIN transactions t
              ON t.instrument_id = i.id AND t.deleted_at IS NULL
            WHERE i.id = %s
            GROUP BY i.id
            """,
            (instrument_id,),
        )
        item = cur.fetchone()
    if item is None:
        raise HTTPException(status_code=404, detail="Instrument not found")
    return Instrument(**item)


@app.get(
    "/api/v1/transactions",
    response_model=Page[Transaction],
    dependencies=[Depends(require_api_key)],
    tags=["transactions"],
)
def list_transactions(
    symbol: str | None = Query(default=None, max_length=100),
    exchange: str | None = Query(default=None, max_length=50),
    transaction_type: Literal["BUY", "SELL"] | None = Query(default=None, alias="type"),
    instrument_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    conn: psycopg.Connection[dict[str, Any]] = Depends(get_connection),
) -> Page[Transaction]:
    where = """
        t.deleted_at IS NULL
        AND (%s::text IS NULL OR upper(i.symbol) = upper(%s))
        AND (%s::text IS NULL OR upper(i.exchange) = upper(%s))
        AND (%s::text IS NULL OR t.type = %s)
        AND (%s::bigint IS NULL OR i.id = %s)
    """
    params = (symbol, symbol, exchange, exchange, transaction_type, transaction_type,
              instrument_id, instrument_id)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*) AS total
            FROM transactions t
            JOIN instruments i ON i.id = t.instrument_id
            WHERE {where}
            """,
            params,
        )
        total = cur.fetchone()["total"]
        cur.execute(
            f"""
            SELECT t.id, t.instrument_id, i.exchange, i.symbol, i.name, t.type,
                   t.quantity, t.price, t.price_derived, t.price_currency,
                   t.invested_amount, t.fees, t.trade_date, t.notes,
                   t.sheet_snapshot, t.source, t.created_at
            FROM transactions t
            JOIN instruments i ON i.id = t.instrument_id
            WHERE {where}
            ORDER BY t.trade_date DESC NULLS LAST, t.created_at DESC, t.id DESC
            LIMIT %s OFFSET %s
            """,
            (*params, limit, offset),
        )
        items = cur.fetchall()
    return Page[Transaction](items=items, total=total, limit=limit, offset=offset)


@app.get(
    "/api/v1/portfolio/holdings",
    response_model=list[Holding],
    dependencies=[Depends(require_api_key)],
    tags=["portfolio"],
)
def get_holdings(
    conn: psycopg.Connection[dict[str, Any]] = Depends(get_connection),
) -> list[Holding]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT i.id AS instrument_id, i.exchange, i.symbol, i.name,
                   COALESCE(t.price_currency, 'UNKNOWN') AS price_currency,
                   SUM(CASE WHEN t.type = 'BUY' THEN t.quantity ELSE -t.quantity END)
                       AS net_quantity,
                   COALESCE(SUM(t.quantity) FILTER (WHERE t.type = 'BUY'), 0)
                       AS buy_quantity,
                   COALESCE(SUM(t.quantity) FILTER (WHERE t.type = 'SELL'), 0)
                       AS sell_quantity,
                   SUM(t.quantity * t.price) FILTER (WHERE t.type = 'BUY')
                       / NULLIF(SUM(t.quantity) FILTER (WHERE t.type = 'BUY'), 0)
                       AS average_buy_price,
                   SUM(t.quantity * t.price) FILTER (WHERE t.type = 'SELL')
                       / NULLIF(SUM(t.quantity) FILTER (WHERE t.type = 'SELL'), 0)
                       AS average_sell_price,
                   COALESCE(SUM(t.invested_amount) FILTER (WHERE t.type = 'BUY'), 0)
                       AS buy_invested_amount_recorded,
                   COALESCE(SUM(t.invested_amount) FILTER (WHERE t.type = 'SELL'), 0)
                       AS sell_invested_amount_recorded
            FROM instruments i
            JOIN transactions t ON t.instrument_id = i.id
            WHERE t.deleted_at IS NULL
            GROUP BY i.id, COALESCE(t.price_currency, 'UNKNOWN')
            HAVING SUM(CASE WHEN t.type = 'BUY' THEN t.quantity ELSE -t.quantity END) <> 0
            ORDER BY i.exchange, i.symbol, price_currency
            """
        )
        items = cur.fetchall()
    return [Holding(**item) for item in items]


@app.get(
    "/api/v1/portfolio/summary",
    response_model=PortfolioSummary,
    dependencies=[Depends(require_api_key)],
    tags=["portfolio"],
)
def get_portfolio_summary(
    conn: psycopg.Connection[dict[str, Any]] = Depends(get_connection),
) -> PortfolioSummary:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)::integer AS transaction_count,
                   COUNT(*) FILTER (WHERE type = 'BUY')::integer AS buy_transaction_count,
                   COUNT(*) FILTER (WHERE type = 'SELL')::integer AS sell_transaction_count,
                   COALESCE(SUM(invested_amount) FILTER (WHERE type = 'BUY'), 0)
                       AS buy_invested_amount_recorded,
                   COALESCE(SUM(invested_amount) FILTER (WHERE type = 'SELL'), 0)
                       AS sell_invested_amount_recorded
            FROM transactions
            WHERE deleted_at IS NULL
            """
        )
        item = cur.fetchone()
    return PortfolioSummary(
        **item,
        invested_amount_currency_note=(
            "The database does not store the currency for invested_amount. "
            "Totals are sums of recorded values and may combine currencies."
        ),
    )


handler = Mangum(app)
