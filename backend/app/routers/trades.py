"""
trades.py — record a trade as a carry-over-basis event.

WHY THIS EXISTS (2026-09-14)
    The `trades` and `trade_items` tables shipped with the original schema and
    NO code has ever touched them. Trades were recorded in the old spreadsheet
    as break-even SALES, which corrupts ROI, sell-through, hold time, and — the
    expensive one — cost basis. A received card with $0 basis reports its whole
    eventual sale price as profit.

    This endpoint makes a trade what it actually is: basis moving, nothing
    realized. `app.trade_basis` owns the math the same way `app.profit` owns
    profit. Neither the router nor the frontend does arithmetic.

⚠️ TRANSACTIONALITY — READ THIS BEFORE TRUSTING IT WITH A BIG TRADE
    PostgREST gives us no cross-table transaction, so this endpoint performs
    several writes in sequence. Ordering is chosen so the least-destructive
    thing happens on a partial failure:

        1. READ + VALIDATE + COMPUTE   (cannot corrupt anything)
        2. INSERT trades               (orphan trade = harmless, no card moved)
        3. INSERT received cards       (new rows, nothing overwritten)
        4. INSERT trade_items          (lineage)
        5. UPDATE given -> traded_away (LAST — the only destructive step)

    So a mid-flight failure leaves extra rows, never lost inventory. Every
    failure logs the trade id so the partial state is findable.

    The correct long-term fix is a Postgres function called via RPC so the
    whole thing is one transaction. Logged in the tech-debt backlog.
"""

import logging
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app import profit, trade_basis
from app.auth import AuthedUser, current_user
from app.routers.cards import Card, CardCreate, grading_costs_by_card
from app.supabase_client import user_client

router = APIRouter(prefix="/trades", tags=["trades"])

log = logging.getLogger(__name__)

Money = Annotated[Decimal, Field(ge=0, le=10_000_000)]
# Boot is the one signed money field in the app: negative means Brady received
# cash. Bounded both ways rather than ge=0.
SignedMoney = Annotated[Decimal, Field(ge=-10_000_000, le=10_000_000)]

TRADED_AWAY = "traded_away"


class ReceivedCardIn(CardCreate):
    """A card coming IN on a trade.

    Inherits every field of CardCreate so a traded-in card is a first-class
    card — same validation, same required fields, same `position_type` rule.

    `est_value` is an ALLOCATION WEIGHT, not a price. It decides how much of
    the carried basis this card absorbs and is never used as a valuation.
    """

    est_value: Optional[Money] = None


class TradeCreate(BaseModel):
    trade_date: date
    given_card_ids: list[str] = Field(min_length=1)
    received: list[ReceivedCardIn] = Field(min_length=1)
    cash_boot: SignedMoney = Decimal(0)
    partner_name: Optional[str] = None
    partner_contact: Optional[str] = None
    notes: Optional[str] = None


class TradeResult(BaseModel):
    trade_id: str
    total_basis: Decimal
    realized_gain: Decimal
    even_split_fallback: bool
    given_card_ids: list[str]
    received_cards: list[Card]
    warnings: list[str] = []


@router.post("", response_model=TradeResult, status_code=status.HTTP_201_CREATED)
def create_trade(
    payload: TradeCreate,
    user: AuthedUser = Depends(current_user),
) -> TradeResult:
    """Record a trade: carry basis from the cards given onto the cards received."""
    client = user_client(user.token)

    # --- 1. READ + VALIDATE + COMPUTE (no writes yet) ----------------------
    given_rows = _fetch_given_cards(client, payload.given_card_ids)

    already_gone = [
        r["id"] for r in given_rows if (r.get("status") or "").lower() == TRADED_AWAY
    ]
    if already_gone:
        # Trading the same card twice would carry its basis into two different
        # cards — basis created from nothing. Refuse rather than double-count.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Already traded away: {', '.join(already_gone)}",
        )

    grading = grading_costs_by_card(client, given_rows)
    outgoing_costs = [
        profit.all_in_cost(row, profit.grading_cost_for(grading.get(str(row.get("id")))))
        for row in given_rows
    ]

    try:
        result = trade_basis.allocate_trade_basis(
            outgoing_all_in_costs=outgoing_costs,
            received=[
                trade_basis.ReceivedCard(ref=str(i), est_value=c.est_value)
                for i, c in enumerate(payload.received)
            ],
            cash_boot=payload.cash_boot,
        )
    except trade_basis.TradeBasisError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )

    warnings: list[str] = []
    if result.even_split_fallback:
        warnings.append(
            "No estimated values supplied, so basis was split evenly. This "
            "distorts per-card ROI — enter an estimated value for each card "
            "received."
        )
    if result.realized_gain > 0:
        warnings.append(
            f"Cash received exceeded the basis given up by ${result.realized_gain}. "
            "Basis cannot go negative, so that excess is recorded as a realized "
            "gain on the trade."
        )

    # --- 2. INSERT the trade ----------------------------------------------
    trade_row = {
        "user_id": user.id,
        "trade_date": payload.trade_date.isoformat(),
        "partner_name": payload.partner_name,
        "partner_contact": payload.partner_contact,
        "notes": payload.notes,
        "cash_boot": str(payload.cash_boot),
        "realized_gain": str(result.realized_gain),
    }
    trade_id = _insert_one(client, "trades", trade_row, "Failed to record trade.")["id"]

    # --- 3. INSERT the received cards -------------------------------------
    received_cards: list[dict] = []
    for i, incoming in enumerate(payload.received):
        row = incoming.model_dump(mode="json", exclude={"est_value"})
        row["user_id"] = user.id
        # The allocated basis IS the card's purchase price. Writing it here
        # means every existing profit surface works on a traded-in card with
        # no changes to app.profit.
        row["purchase_price"] = str(result.allocations[str(i)])
        row["purchase_date"] = payload.trade_date.isoformat()
        row["acquisition_source"] = "trade"
        received_cards.append(
            _insert_one(client, "cards", row, "Failed to create a traded-in card.")
        )

    # --- 4. INSERT lineage -------------------------------------------------
    for row in given_rows:
        _insert_one(
            client,
            "trade_items",
            {"trade_id": trade_id, "card_id": row["id"], "direction": "given"},
            "Failed to record a traded-away line item.",
        )
    for i, card in enumerate(received_cards):
        _insert_one(
            client,
            "trade_items",
            {
                "trade_id": trade_id,
                "card_id": card["id"],
                "direction": "received",
                "est_value": _opt_str(payload.received[i].est_value),
                "allocated_basis": str(result.allocations[str(i)]),
            },
            "Failed to record a traded-in line item.",
        )

    # --- 5. UPDATE given cards LAST (the only destructive step) ------------
    for row in given_rows:
        try:
            client.table("cards").update({"status": TRADED_AWAY}).eq(
                "id", row["id"]
            ).execute()
        except Exception:
            log.exception(
                "Trade %s recorded but card %s was NOT marked traded_away. "
                "Inventory now overstates by this card.",
                trade_id,
                row["id"],
            )
            warnings.append(
                f"Card {row['id']} could not be marked traded away — fix it "
                "manually or inventory will double-count."
            )

    enriched = [
        profit.enrich(c, grading_costs_by_card(client, [c]).get(str(c.get("id"))))
        for c in received_cards
    ]

    return TradeResult(
        trade_id=str(trade_id),
        total_basis=result.total_basis,
        realized_gain=result.realized_gain,
        even_split_fallback=result.even_split_fallback,
        given_card_ids=[str(r["id"]) for r in given_rows],
        received_cards=enriched,
        warnings=warnings,
    )


def _fetch_given_cards(client, card_ids: list[str]) -> list[dict]:
    """Load the outgoing cards, RLS-scoped. Any id we can't see is a 404.

    Deliberately does NOT distinguish "no such card" from "someone else's
    card" — same reasoning as the cards router, where confirming another
    user's row exists is itself a leak.
    """
    rows: list[dict] = []
    for cid in card_ids:
        try:
            resp = client.table("cards").select("*").eq("id", cid).execute()
        except Exception:
            log.exception("Trade lookup failed (card_id=%r)", cid)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to load cards for the trade.",
            )
        if not resp.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Card not found: {cid}",
            )
        rows.append(resp.data[0])

    seen = {r["id"] for r in rows}
    if len(seen) != len(card_ids):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The same card was listed twice in one trade.",
        )
    return rows


def _insert_one(client, table: str, row: dict, err: str) -> dict:
    try:
        resp = client.table(table).insert(row).execute()
    except Exception:
        log.exception("Insert into %s failed", table)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=err)
    if not resp.data:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=err)
    return resp.data[0]


def _opt_str(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else str(value)
