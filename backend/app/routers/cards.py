"""
cards.py

The inventory endpoints for the cards table.

Every query goes through user_client(token), so RLS only lets each user see
their own cards. I don't just rely on a WHERE clause for that, the database
handles it.
"""

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, BeforeValidator, Field

from app import profit
from app.auth import AuthedUser, current_user
from app.supabase_client import user_client

router = APIRouter(prefix="/cards", tags=["cards"])

log = logging.getLogger(__name__)

# Money can't be negative or crazy high. Bad values get a 422.
Money = Annotated[Decimal, Field(ge=0, le=10_000_000)]

# The allowed values below match the CHECK constraints from migration 007. If
# something bad gets sent, it fails here with a clear 422 instead of a confusing
# Postgres error (23514). That's what broke saving cards back in August. Keep
# these matching the migration, and keep them lowercase.
def _blank_to_none(value):
    """Trim a text field and turn it into None if it ends up empty.

    The serial column (migration 010) doesn't allow '' or extra spaces. An empty
    box in the browser sends "" instead of null, so without this, leaving the
    serial blank would make the save fail.

    Blank just means the card isn't numbered, which is normal, so it shouldn't
    be an error. On PATCH this is also how you clear a serial you typed wrong.
    """
    if isinstance(value, str):
        return value.strip() or None
    return value


# The serial / print run exactly how it's printed ("9/25", "1/1", "FOTL 12/99").
# Same length limit as migration 010, so a value that's too long gets a 422
# here. It's text and not two numbers because a lot of serials don't fit that
# (see the migration).
#
# Note: max_length has to go on the inner str, not the Optional. If it's on the
# Optional, pydantic errors on None, and clearing a serial would crash with a
# 500. test_clearing_a_serial_is_expressible_on_patch checks this.
Serial = Annotated[
    Optional[Annotated[str, Field(max_length=32)]],
    BeforeValidator(_blank_to_none),
]

PositionType = Literal["flip", "hold"]
Lane = Literal["graded_arb", "raw_to_grade", "sealed", "optcg", "other"]
SaleChannel = Literal["discord", "facebook", "instagram", "ebay", "show", "other"]


class Card(BaseModel):
    """A row from the cards table, plus the profit numbers from app.profit.

    The fields at the bottom aren't saved in the database. They get calculated
    every time cards are loaded so the profit math only exists in one place.
    The frontend just shows them.
    """

    id: str
    user_id: Optional[str] = None
    player: str
    year: str
    set_name: str
    card_number: Optional[str] = None
    # --- migration 010: the print run as printed. None = not numbered ---
    serial: Optional[str] = None
    category: Optional[str] = None
    card_type: Optional[str] = None
    purchase_price: Optional[Decimal] = None
    purchase_date: Optional[date] = None
    sale_price: Optional[Decimal] = None
    sale_date: Optional[date] = None
    status: Optional[str] = None
    ebay_comp_url: Optional[str] = None
    image_url: Optional[str] = None
    notes: Optional[str] = None
    acquisition_source: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    # --- migration 007: costs when buying ---
    shipping_in: Optional[Decimal] = None
    purchase_tax: Optional[Decimal] = None
    other_costs: Optional[Decimal] = None

    # --- migration 007: selling. shipping_collected counts as money in ---
    shipping_out: Optional[Decimal] = None
    platform_fees: Optional[Decimal] = None
    shipping_collected: Optional[Decimal] = None
    sale_channel: Optional[str] = None
    buyer_name: Optional[str] = None

    # --- migration 007: strategy and reporting fields ---
    position_type: Optional[str] = None
    lane: Optional[str] = None
    est_market_value: Optional[Decimal] = None
    hold_thesis: Optional[str] = None

    # --- calculated by app.profit, not saved ---
    grading_cost: Optional[Decimal] = None
    all_in_cost: Optional[Decimal] = None
    net_proceeds: Optional[Decimal] = None
    # None until sold, not 0. A 0 would get averaged into ROI and pull it down.
    net_profit: Optional[Decimal] = None
    roi: Optional[Decimal] = None
    hold_days: Optional[int] = None


class CardCreate(BaseModel):
    """What gets sent when I add a card by hand. user_id isn't taken from the
    request. It comes from the token, so nobody can make a card under someone
    else's account (RLS would block that anyway).
    """

    # Required (can't be empty in the table).
    player: str = Field(min_length=1)
    year: str = Field(min_length=1)
    set_name: str = Field(min_length=1)
    category: str = Field(min_length=1)  # basketball/football/one piece/pokemon/...

    # Optional.
    card_number: Optional[str] = None
    # This is NOT the card number. It's the print run stamped on the card
    # ("9/25" means card 9 of 25). Leave it empty if the card isn't numbered,
    # which is most of them.
    serial: Serial = None
    card_type: Optional[str] = None  # finish/parallel: refractor, blue refractor, ... (user-picked)
    # Just the price of the card. Shipping and tax have their own fields below.
    # In my old spreadsheet this was the all-in number, so if I put the all-in
    # here it would count shipping and tax twice. The form labels this.
    purchase_price: Optional[Money] = None
    purchase_date: Optional[date] = None
    sale_price: Optional[Money] = None
    sale_date: Optional[date] = None
    status: str = "in_hand"
    ebay_comp_url: Optional[str] = None
    image_url: Optional[str] = None
    notes: Optional[str] = None
    acquisition_source: Optional[str] = None

    # --- migration 007 ---
    shipping_in: Optional[Money] = None
    purchase_tax: Optional[Money] = None
    other_costs: Optional[Money] = None
    shipping_out: Optional[Money] = None
    platform_fees: Optional[Money] = None
    shipping_collected: Optional[Money] = None
    sale_channel: Optional[SaleChannel] = None
    buyer_name: Optional[str] = None
    est_market_value: Optional[Money] = None
    hold_thesis: Optional[str] = None
    lane: Optional[Lane] = None

    # Can only be set when the card is created. It's left out of CardUpdate on
    # purpose so flip vs hold gets decided once, when I buy it.
    position_type: PositionType = "flip"


@router.post("", response_model=Card, status_code=status.HTTP_201_CREATED)
def create_card(
    payload: CardCreate,
    user: AuthedUser = Depends(current_user),
) -> dict:
    """
    Add a card for the logged in user. The owner comes from the token, and RLS
    double checks that user_id = auth.uid().
    """
    row = payload.model_dump(mode="json", exclude_none=True)
    row["user_id"] = user.id

    client = user_client(user.token)
    try:
        resp = client.table("cards").insert(row).execute()
    except Exception:
        # Log the actual error so I can debug it, but only send a plain message
        # back to the browser so no database details get out.
        log.exception("Card insert failed. Payload keys: %s", sorted(row))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to create card.",
        )

    if not resp.data:
        log.error("Card insert returned no rows. Payload keys: %s", sorted(row))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Card was not created.",
        )
    return resp.data[0]


class CardUpdate(BaseModel):
    """What gets sent when I edit a card.

    Every field is optional and only the fields actually sent get changed (see
    exclude_unset in update_card). Sending {} gets rejected.

    id, user_id, created_at and updated_at can't be changed. They're just not
    in this model, so there's no way to even try to change who owns a card.

    position_type can't be changed either.

    My rule is that a card is a flip or a hold from the day I buy it. If a flip
    doesn't sell, that's a loss. It doesn't get to become part of my
    collection. If I could switch it, it would be way too easy to move bad
    flips into the collection, and then my ROI would look better than it
    really is while money sits in cards nobody wants.

    Any field in this model can be edited, so leaving it out is what blocks
    it. Don't add position_type here. If I really labeled a card wrong when I
    added it, I'll fix it in the database directly.
    """

    player: Optional[str] = Field(default=None, min_length=1)
    year: Optional[str] = Field(default=None, min_length=1)
    set_name: Optional[str] = Field(default=None, min_length=1)
    category: Optional[str] = Field(default=None, min_length=1)
    card_number: Optional[str] = None
    # This one can be edited. A serial that got read wrong is just a typo.
    # Sending "" clears it (see _blank_to_none).
    serial: Serial = None
    card_type: Optional[str] = None
    purchase_price: Optional[Money] = None
    purchase_date: Optional[date] = None
    sale_price: Optional[Money] = None
    sale_date: Optional[date] = None
    status: Optional[str] = None
    ebay_comp_url: Optional[str] = None
    # This is a path in the private card-images bucket, not a link. Something
    # like "{user_id}/{card_id}/front.jpg". The frontend makes a temporary
    # signed link when it shows the image. Saving a signed link here wouldn't
    # work since they expire.
    image_url: Optional[str] = None
    notes: Optional[str] = None
    acquisition_source: Optional[str] = None

    # --- migration 007 (no position_type on purpose, see above) ---
    shipping_in: Optional[Money] = None
    purchase_tax: Optional[Money] = None
    other_costs: Optional[Money] = None
    shipping_out: Optional[Money] = None
    platform_fees: Optional[Money] = None
    shipping_collected: Optional[Money] = None
    sale_channel: Optional[SaleChannel] = None
    buyer_name: Optional[str] = None
    est_market_value: Optional[Money] = None
    hold_thesis: Optional[str] = None
    lane: Optional[Lane] = None


@router.patch("/{card_id}", response_model=Card)
def update_card(
    card_id: str,
    payload: CardUpdate,
    user: AuthedUser = Depends(current_user),
) -> dict:
    """
    Edit one of the logged in user's cards.

    RLS means the update can't touch anyone else's card, so a wrong card_id
    just comes back as a 404. updated_at gets set by a trigger in the database.
    """
    changes = payload.model_dump(mode="json", exclude_unset=True)
    if not changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No fields to update.",
        )

    client = user_client(user.token)
    try:
        resp = client.table("cards").update(changes).eq("id", card_id).execute()
    except Exception:
        # Same as create_card. Log the real error, send back a plain message.
        log.exception(
            "Card update failed (card_id=%r). Changed keys: %s",
            card_id,
            sorted(changes),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to update card.",
        )

    if not resp.data:
        # Either the card doesn't exist or it's someone else's. Both are a 404 so
        # nobody can tell if another user's card exists.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Card not found.",
        )
    return resp.data[0]


@router.get("", response_model=list[Card])
def list_cards(
    user: AuthedUser = Depends(current_user),
    status_filter: Optional[str] = Query(
        None,
        alias="status",
        description=(
            "Filter by exact status, e.g. 'sold' for the Sold tab. "
            "Omit to return all of the user's cards."
        ),
    ),
) -> list[dict]:
    """
    Get the logged in user's cards, newest first. RLS makes sure only their
    cards come back.
    """
    client = user_client(user.token)
    query = client.table("cards").select("*").order("created_at", desc=True)
    if status_filter is not None:
        query = query.eq("status", status_filter)

    try:
        resp = query.execute()
    except Exception:
        # Log the real error and send back a plain message. Without the log I'd
        # have no idea what went wrong in production.
        log.exception("GET /cards failed (status_filter=%r)", status_filter)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to fetch cards.",
        )

    rows = resp.data or []
    return profit.enrich_many(rows, grading_costs_by_card(client, rows))


def grading_costs_by_card(client, rows: list[dict]) -> dict[str, list[dict]]:
    """Get the grading submissions for these cards in one query, by card_id.

    Grading cost wasn't part of profit before this. For a card I buy raw and
    send in, it's usually the biggest cost after the card itself.

    If this fails it doesn't break anything. The cards still load with
    grading_cost = 0. The profit would be a little high, so it gets logged, but
    that's better than the whole inventory not loading.
    """
    ids = [str(r["id"]) for r in rows if r.get("id")]
    if not ids:
        return {}

    try:
        resp = (
            client.table("grading_submissions")
            .select("card_id, cost")
            .in_("card_id", ids)
            .execute()
        )
    except Exception:
        log.exception(
            "Could not load grading submissions for %d cards; "
            "profit will EXCLUDE grading cost and read high.",
            len(ids),
        )
        return {}

    by_card: dict[str, list[dict]] = {}
    for sub in resp.data or []:
        by_card.setdefault(str(sub.get("card_id")), []).append(sub)
    return by_card


class CardClose(BaseModel):
    """What gets sent when I mark a card as sold.

    The app isn't where I sell. I sell on eBay, Whatnot, Discord, etc. This is
    just logging the sale after it happens.

    Fees are required. If I don't enter them right when I log the sale, I'm
    never going to go back and look up an eBay fee from three weeks ago, and
    all my profit numbers would be too high again.

    So platform_fees and shipping_out don't have defaults, and leaving them out
    is a 422. If there weren't any fees (Discord, cash at a show), I send 0.

    This is its own endpoint instead of using PATCH because PATCH can't make
    anything required.
    """

    sale_price: Money
    sale_date: date
    platform_fees: Money
    shipping_out: Money
    # What the buyer paid me for shipping. Counts as money in.
    shipping_collected: Money = Decimal(0)
    sale_channel: Optional[SaleChannel] = None
    buyer_name: Optional[str] = None


@router.post("/{card_id}/close", response_model=Card)
def close_card(
    card_id: str,
    payload: CardClose,
    user: AuthedUser = Depends(current_user),
) -> dict:
    """
    Mark a card as sold and save the sale details.

    This is what feeds the P&L page. Without it a card would sit in_hand
    forever.

    It also clears est_market_value. Once I have the real sale price, the old
    estimate doesn't matter.
    """
    changes = payload.model_dump(mode="json")
    changes["status"] = "sold"
    changes["est_market_value"] = None

    client = user_client(user.token)
    try:
        resp = client.table("cards").update(changes).eq("id", card_id).execute()
    except Exception:
        log.exception("Card close-out failed (card_id=%r)", card_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to close out card.",
        )

    if not resp.data:
        # Card doesn't exist or isn't theirs. Both are a 404 on purpose.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Card not found.",
        )

    row = resp.data[0]
    return profit.enrich(row, grading_costs_by_card(client, [row]).get(str(row.get("id"))))
