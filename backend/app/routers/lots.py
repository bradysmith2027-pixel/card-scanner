"""
lots.py

Saves a lot purchase and splits the cost across the cards in it.

Lots have been my best ROI (around 39% vs about 6% on single cards), and before
this I couldn't log one properly. I was just guessing a cost for each card.

The math is all in app.lot_basis. This file just saves things.

How I actually enter a lot: I buy 50 cards for $200, enter the 4 worth
tracking, and box the rest. bulk_remainder_value is my guess at what the rest
are worth together. It takes its share of the cost so the 4 cards I entered
don't get charged for the whole lot.

Supabase's API can't do one transaction across tables, so I save the lot first
and then the cards. If something fails partway, I end up with a lot that's
missing some cards (easy to spot and fix) instead of cards that aren't
connected to any lot. The check query in migration 009 finds those.
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
    """A card from the lot that I'm entering on its own.

    It uses CardCreate so it's a normal card. If purchase_price gets sent it's
    ignored, since the split decides it.
    """

    est_value: Optional[Money] = None


class LotCreate(BaseModel):
    purchase_date: date
    # Just the price of the lot. Shipping, tax and other costs have their own
    # fields, same as for single cards. Putting the all-in number here would
    # count them twice.
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
    """Save a lot and create its cards with their share of the cost."""
    # --- do the math first, nothing gets saved unless it works ------------
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

    # If the lot has more cards than I entered and there's no bulk value, the
    # cards I didn't enter aren't taking any of the cost, so the ones I did
    # enter are carrying all of it. Worth a warning.
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
        # The card's share of the lot becomes its purchase price, so all the
        # profit stuff works on lot cards without any changes.
        row["purchase_price"] = str(result.allocations[str(i)])
        row["purchase_date"] = payload.purchase_date.isoformat()
        row.setdefault("acquisition_source", None)
        if not row.get("acquisition_source"):
            row["acquisition_source"] = "purchase"
        # The lot's shipping and tax are already in the split, so they get
        # cleared here or they'd be counted twice.
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
