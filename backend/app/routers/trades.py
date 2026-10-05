"""
trades.py

Saves a trade and moves the cost from the cards I gave up onto the cards I got.

The trades and trade_items tables were in my schema from the start but nothing
used them. In my spreadsheet I logged trades as sales that broke even, which
messed up my ROI, sell-through, hold time, and worst of all the cost of the
card I got back. A card with a $0 cost shows its whole sale price as profit.

Now a trade just moves the cost over and nothing counts as profit. The math is
in app.trade_basis. This file and the frontend don't do any math.

Supabase's API can't do one transaction across tables, so this saves things
one step at a time, in an order where a failure partway through does the least
damage:

    1. Read, check and do the math   (can't break anything)
    2. Save the trade                 (if it stops here, no cards moved)
    3. Save the cards I got           (new rows, nothing overwritten)
    4. Save trade_items               (links everything together)
    5. Mark my cards as traded_away   (last, since it's the only step that changes existing cards)

So if something fails I might end up with extra rows, but I never lose cards.
Every failure logs the trade id so I can find it.

The real fix would be a Postgres function that does it all in one transaction.
It's on my to do list.
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
# Cash boot is the only money field that can be negative. Negative means I got
# cash back. So it has a limit both ways instead of just >= 0.
SignedMoney = Annotated[Decimal, Field(ge=-10_000_000, le=10_000_000)]

TRADED_AWAY = "traded_away"


class ReceivedCardIn(CardCreate):
    """A card I'm getting in the trade.

    It uses all of CardCreate, so it's a normal card with the same checks and
    the same position_type rule.

    est_value isn't a price. It just decides how much of the cost this card
    takes.
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
    """Save a trade and move the cost from the cards I gave onto the cards I got."""
    client = user_client(user.token)

    # --- 1. Read, check and do the math (nothing saved yet) ----------------
    given_rows = _fetch_given_cards(client, payload.given_card_ids)

    already_gone = [
        r["id"] for r in given_rows if (r.get("status") or "").lower() == TRADED_AWAY
    ]
    if already_gone:
        # If the same card is in the trade twice, its cost would get counted
        # twice. Don't allow it.
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

    # --- 2. Save the trade ------------------------------------------------
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

    # --- 3. Save the cards I got -------------------------------------------
    received_cards: list[dict] = []
    for i, incoming in enumerate(payload.received):
        row = incoming.model_dump(mode="json", exclude={"est_value"})
        row["user_id"] = user.id
        # The card's share of the cost becomes its purchase price, so the
        # profit stuff works on traded cards without changing app.profit.
        row["purchase_price"] = str(result.allocations[str(i)])
        row["purchase_date"] = payload.trade_date.isoformat()
        row["acquisition_source"] = "trade"
        received_cards.append(
            _insert_one(client, "cards", row, "Failed to create a traded-in card.")
        )

    # --- 4. Save trade_items ----------------------------------------------
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

    # --- 5. Mark the cards I gave as traded_away (last on purpose) --------
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
    """Load the cards I'm giving up. Any id I can't see is a 404.

    Doesn't say whether the card doesn't exist or belongs to someone else,
    same as in the cards router.
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
