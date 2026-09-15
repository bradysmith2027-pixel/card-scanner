"""
lots.py — record a bundle buy and spread its cost across the cards inside.

WHY THIS EXISTS (2026-09-14)
    Brady's most profitable channel is Discord lots (39% ROI vs 5.7% on single
    cards bought near comp), and until now the app could not record one. Every
    lot card got a hand-guessed basis, which makes per-card ROI — the number
    the T1 tranche gates read — meaningless in exactly the lane that matters
    most.

    `app.lot_basis` owns the allocation the same way `app.profit` owns profit.
    This router does no arithmetic.

THE SHAPE OF A REAL LOT ENTRY
    Brady buys 50 cards for $200, enters the 4 worth tracking, and boxes the
    rest. `bulk_remainder_value` is his estimate of what those un-entered
    cards are worth in total; it absorbs its proportional share of the cost so
    the 4 entered cards are not overcharged for the whole lot.

⚠️ TRANSACTIONALITY
    Same constraint as trades: PostgREST offers no cross-table transaction.
    Order is lot -> cards, so a mid-flight failure leaves a lot with fewer
    cards than intended (visible and fixable) rather than cards with no lot
    (invisible). The balance-check query in migration 009 finds any such lot.
"""

import logging
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app import lot_basis, profit
from app.auth import AuthedUser, current_user
from app.routers.cards import Card, CardCreate, grading_costs_by_card
from app.supabase_client import user_client

router = APIRouter(prefix="/lots", tags=["lots"])

log = logging.getLogger(__name__)

Money = Annotated[Decimal, Field(ge=0, le=10_000_000)]

LotSource = Literal[
    "discord", "facebook", "instagram", "ebay",
    "whatnot", "show", "private_seller", "other",
]


class LotCardIn(CardCreate):
    """A card being entered individually from the lot.

    Inherits CardCreate so a lot card is a first-class card. `purchase_price`
    is IGNORED if sent — the whole point is that the allocator decides it.
    """

    est_value: Optional[Money] = None


class LotCreate(BaseModel):
    purchase_date: date
    # ⚠️ The lot PRICE only. Shipping/tax/other are separate, matching the
    # card-level convention from migration 007. Entering an all-in number
    # here double-counts, silently.
    total_cost: Money
    cards: list[LotCardIn] = Field(min_length=1)

    shipping_in: Optional[Money] = None
    purchase_tax: Optional[Money] = None
    other_costs: Optional[Money] = None

    bulk_remainder_value: Money = Decimal(0)
    card_count: Optional[int] = Field(default=None, ge=1)
    source: Optional[LotSource] = None
    seller_name: Optional[str] = None
    notes: Optional[str] = None


class LotResult(BaseModel):
    lot_id: str
    lot_all_in: Decimal
    bulk_basis: Decimal
    even_split_fallback: bool
    cards: list[Card]
    warnings: list[str] = []


@router.post("", response_model=LotResult, status_code=status.HTTP_201_CREATED)
def create_lot(
    payload: LotCreate,
    user: AuthedUser = Depends(current_user),
) -> LotResult:
    """Record a lot purchase and create its cards with allocated cost basis."""
    # --- compute first; nothing is written until the math succeeds ---------
    try:
        result = lot_basis.allocate_lot_basis(
            lot=payload.model_dump(mode="json"),
            cards=[
                lot_basis.LotCard(ref=str(i), est_value=c.est_value)
                for i, c in enumerate(payload.cards)
            ],
            bulk_remainder_value=payload.bulk_remainder_value,
        )
    except lot_basis.LotBasisError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )

    warnings = list(result.warnings)

    # A stated card_count that exceeds what was entered, with no bulk value,
    # means the un-entered cards are silently absorbing nothing — so the
    # entered ones are carrying their cost. Worth saying out loud.
    if (
        payload.card_count
        and payload.card_count > len(payload.cards)
        and payload.bulk_remainder_value == 0
    ):
        missing = payload.card_count - len(payload.cards)
        warnings.append(
            f"You said this lot had {payload.card_count} cards but entered "
            f"{len(payload.cards)}. The other {missing} were given no value, so "
            "the entered cards absorbed the entire lot cost and their ROI will "
            "look worse than reality. Set bulk_remainder_value."
        )

    client = user_client(user.token)

    lot_row = {
        "user_id": user.id,
        "purchase_date": payload.purchase_date.isoformat(),
        "total_cost": str(payload.total_cost),
        "shipping_in": _opt(payload.shipping_in),
        "purchase_tax": _opt(payload.purchase_tax),
        "other_costs": _opt(payload.other_costs),
        "bulk_remainder_value": str(payload.bulk_remainder_value),
        "bulk_basis": str(result.bulk_basis),
        "card_count": payload.card_count,
        "source": payload.source,
        "seller_name": payload.seller_name,
        "notes": payload.notes,
    }
    lot_id = _insert_one(client, "purchase_lots", lot_row, "Failed to record lot.")["id"]

    created: list[dict] = []
    for i, incoming in enumerate(payload.cards):
        row = incoming.model_dump(mode="json", exclude={"est_value"})
        row["user_id"] = user.id
        row["lot_id"] = lot_id
        # The allocated share IS the purchase price. Writing it here means
        # every existing profit surface works on a lot card unchanged.
        row["purchase_price"] = str(result.allocations[str(i)])
        row["purchase_date"] = payload.purchase_date.isoformat()
        row.setdefault("acquisition_source", None)
        if not row.get("acquisition_source"):
            row["acquisition_source"] = "purchase"
        # Lot-level costs are already inside the allocated basis. Leaving
        # per-card shipping/tax populated here would double-count them.
        row["shipping_in"] = None
        row["purchase_tax"] = None
        created.append(
            _insert_one(client, "cards", row, "Failed to create a card from the lot.")
        )

    enriched = [
        profit.enrich(c, grading_costs_by_card(client, [c]).get(str(c.get("id"))))
        for c in created
    ]

    return LotResult(
        lot_id=str(lot_id),
        lot_all_in=result.lot_all_in,
        bulk_basis=result.bulk_basis,
        even_split_fallback=result.even_split_fallback,
        cards=enriched,
        warnings=warnings,
    )


def _insert_one(client, table: str, row: dict, err: str) -> dict:
    try:
        resp = client.table(table).insert(row).execute()
    except Exception:
        log.exception("Insert into %s failed", table)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=err)
    if not resp.data:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=err)
    return resp.data[0]


def _opt(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else str(value)
