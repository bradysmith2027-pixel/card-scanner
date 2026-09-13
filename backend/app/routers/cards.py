"""
cards.py — inventory endpoints backed by the `cards` table.

Every query goes through user_client(token), so Supabase RLS scopes results to
the logged-in user automatically. The backend never relies on a WHERE clause
alone for isolation — the database is the backstop (see the schema design doc).
"""

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app import profit
from app.auth import AuthedUser, current_user
from app.supabase_client import user_client

router = APIRouter(prefix="/cards", tags=["cards"])

log = logging.getLogger(__name__)

# Money must be non-negative and within sane bounds (server-side validation,
# per the schema design doc's security requirements). Bad values -> 422.
Money = Annotated[Decimal, Field(ge=0, le=10_000_000)]

# These mirror the CHECK constraints added in migration 007. Declaring them as
# Literals means a bad value fails as a clean 422 at the API boundary instead of
# reaching Postgres and coming back as an opaque 23514 — which is exactly how
# the 2026-08-18 outage happened (a free-text field against a constrained
# column). Keep these in sync with the migration; all values are lowercase.
PositionType = Literal["flip", "hold"]
Lane = Literal["graded_arb", "raw_to_grade", "sealed", "optcg", "other"]
SaleChannel = Literal["discord", "facebook", "instagram", "ebay", "show", "other"]


class Card(BaseModel):
    """Shape of a row in the `cards` table (mirrors the columns as built),
    plus the profit fields computed server-side by `app.profit`.

    The computed block at the bottom is NOT stored. It is derived on read so
    that exactly one implementation of the profit math exists — the frontend
    renders these values and does no arithmetic of its own.
    """

    id: str
    user_id: Optional[str] = None
    player: str
    year: str
    set_name: str
    card_number: Optional[str] = None
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

    # --- migration 007: cost inputs (buy side) ---
    shipping_in: Optional[Decimal] = None
    purchase_tax: Optional[Decimal] = None
    other_costs: Optional[Decimal] = None

    # --- migration 007: sell side. shipping_collected is REVENUE. ---
    shipping_out: Optional[Decimal] = None
    platform_fees: Optional[Decimal] = None
    shipping_collected: Optional[Decimal] = None
    sale_channel: Optional[str] = None
    buyer_name: Optional[str] = None

    # --- migration 007: strategy + reporting dimensions ---
    position_type: Optional[str] = None
    lane: Optional[str] = None
    est_market_value: Optional[Decimal] = None
    hold_thesis: Optional[str] = None

    # --- computed by app.profit, never stored ---
    grading_cost: Optional[Decimal] = None
    all_in_cost: Optional[Decimal] = None
    net_proceeds: Optional[Decimal] = None
    # None (not 0) until sold — 0 would average into ROI and drag it down.
    net_profit: Optional[Decimal] = None
    roi: Optional[Decimal] = None
    hold_days: Optional[int] = None


class CardCreate(BaseModel):
    """Payload for manually entering a card. `user_id` is NOT accepted from the
    client — it's set server-side from the verified token, so a caller can't
    create rows owned by someone else (RLS's WITH CHECK would reject it anyway).
    """

    # Required (NON-NULL in the table).
    player: str = Field(min_length=1)
    year: str = Field(min_length=1)
    set_name: str = Field(min_length=1)
    category: str = Field(min_length=1)  # basketball/football/one piece/pokemon/...

    # Optional.
    card_number: Optional[str] = None
    card_type: Optional[str] = None  # finish/parallel: refractor, blue refractor, ... (user-picked)
    # ⚠️ CARD PRICE ONLY — shipping and tax are separate fields below. This
    # differs from the spreadsheet-era convention where purchase_price was
    # all-in. Entering an all-in number here double-counts and silently
    # overstates cost basis. The entry form must label this clearly.
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

    # 🔒 Settable ONLY at creation. It is deliberately absent from CardUpdate,
    # so this is the one and only moment a card's bucket is decided.
    position_type: PositionType = "flip"


@router.post("", response_model=Card, status_code=status.HTTP_201_CREATED)
def create_card(
    payload: CardCreate,
    user: AuthedUser = Depends(current_user),
) -> dict:
    """
    Create a card for the current user. Ownership is stamped from the verified
    token, and RLS's WITH CHECK enforces user_id = auth.uid() as the backstop.
    """
    row = payload.model_dump(mode="json", exclude_none=True)
    row["user_id"] = user.id

    client = user_client(user.token)
    try:
        resp = client.table("cards").insert(row).execute()
    except Exception:
        # Log the real cause server-side (constraint violation, RLS rejection,
        # unknown column, ...) but keep the client message generic — DB detail
        # must not leak to the browser. Without this the 502 is undebuggable.
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
    """Partial update payload.

    Every field is optional; only the keys actually present in the request body
    are written (see `exclude_unset` in `update_card`). Sending `{}` is rejected
    rather than silently doing nothing.

    Deliberately NOT updatable: `id`, `user_id`, `created_at`, `updated_at`.
    Ownership must never be reassignable from the client — RLS's WITH CHECK
    would reject it anyway, but the field is omitted so the attempt can't even
    be expressed.

    🔒 ALSO NOT UPDATABLE: `position_type`.

    The business rule is that a card enters the hold bucket AT PURCHASE and is
    never reclassified. A flip that did not sell is a LOSS, not a collection
    piece.

    This is enforced structurally rather than by policy because the failure
    mode needs no bad intent: with flips and holds funded from the same
    capital, "personal collection" is the perfect place to park a failed flip.
    Reclassify one and the position leaves the performance report, ROI looks
    clean, and the monthly review shows profit while capital sits frozen in
    cards nobody wanted.

    Since `update_card` uses `exclude_unset`, ANY field present on this model
    is patchable — so omission is the enforcement. Do not add position_type
    here. If a card was genuinely mislabelled at creation, fix it in the
    database directly and note why.
    """

    player: Optional[str] = Field(default=None, min_length=1)
    year: Optional[str] = Field(default=None, min_length=1)
    set_name: Optional[str] = Field(default=None, min_length=1)
    category: Optional[str] = Field(default=None, min_length=1)
    card_number: Optional[str] = None
    card_type: Optional[str] = None
    purchase_price: Optional[Money] = None
    purchase_date: Optional[date] = None
    sale_price: Optional[Money] = None
    sale_date: Optional[date] = None
    status: Optional[str] = None
    ebay_comp_url: Optional[str] = None
    # Storage PATH inside the private `card-images` bucket, not a public URL —
    # e.g. "{user_id}/{card_id}/front.jpg". The bucket is private, so the client
    # mints a short-lived signed URL at render time. Storing a signed URL here
    # would be wrong: it expires, and the row would rot.
    image_url: Optional[str] = None
    notes: Optional[str] = None
    acquisition_source: Optional[str] = None

    # --- migration 007 (position_type intentionally absent, see docstring) ---
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
    Partially update one of the current user's cards.

    RLS is the isolation boundary: the UPDATE simply cannot match a row owned by
    someone else, so a wrong/guessed `card_id` returns 404 rather than touching
    another user's data. `updated_at` is maintained by the DB trigger.
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
        # Same reasoning as create_card: log the real cause (CHECK violation,
        # RLS rejection, bad column) server-side, keep the client message
        # generic so no DB detail leaks.
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
        # Either no such card, or it belongs to someone else. Both are 404 on
        # purpose — distinguishing them would confirm the existence of another
        # user's row.
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
    List the current user's cards, newest first. RLS guarantees only this
    user's rows come back regardless of the query.
    """
    client = user_client(user.token)
    query = client.table("cards").select("*").order("created_at", desc=True)
    if status_filter is not None:
        query = query.eq("status", status_filter)

    try:
        resp = query.execute()
    except Exception:
        # Log the real cause server-side, return a generic message to the
        # client (no internal/DB detail leaked). Without the log this branch is
        # undebuggable in production — the same gap create_card had until
        # 2026-08-18, which is what made the 23514 constraint bug a mystery.
        log.exception("GET /cards failed (status_filter=%r)", status_filter)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to fetch cards.",
        )

    rows = resp.data or []
    return profit.enrich_many(rows, grading_costs_by_card(client, rows))


def grading_costs_by_card(client, rows: list[dict]) -> dict[str, list[dict]]:
    """Fetch grading submissions for these cards in ONE query, keyed by card_id.

    This is the join that was missing from every profit number in the app:
    `grading_submissions.cost` has existed since the initial schema and was
    never included anywhere. For a raw-to-grade card it is often the second
    largest cost after the card itself.

    Failure here is deliberately NON-FATAL. If the grading table can't be read,
    every card still returns with grading_cost = 0 rather than the whole
    inventory 502-ing. The profit numbers would be slightly optimistic, which
    is bad — so it is logged loudly — but an unreadable inventory is worse.
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
    """Close-out payload: recording a sale that already happened elsewhere.

    ⚠️ This app is NOT a storefront. Brady sells on eBay/Whatnot/Discord and
    handles that himself. This endpoint is ~30 seconds of data entry after the
    fact, nothing more.

    WHY FEES ARE REQUIRED AND NOT OPTIONAL
        Fees not captured at the moment of close-out are never captured. Nobody
        reconstructs an eBay cut from three weeks ago. An optional field here
        would be left blank, migration 007's cost columns would sit empty
        forever, and every profit number would quietly revert to being gross.

        So `platform_fees` and `shipping_out` have no defaults — omitting them
        is a 422. Selling somewhere with no fees (Discord, cash at a show) is
        expressed by explicitly sending 0, which is a statement rather than an
        omission.

    WHY A DEDICATED ENDPOINT INSTEAD OF PATCH
        PATCH uses `exclude_unset` and cannot require anything. Only a separate
        model can make fees mandatory — which is the entire point.
    """

    sale_price: Money
    sale_date: date
    platform_fees: Money
    shipping_out: Money
    # Revenue: what the buyer paid for shipping on top of the card price.
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
    Close out a card: record the sale and flip status to 'sold'.

    This is what makes the P&L real. Until a card can be closed it never leaves
    `in_hand`, so the dashboard has no inputs and time-in-hand never stops.

    Sets `est_market_value` to NULL on close (Brady's convention): once there is
    a real sale price, a stale estimate beside it is just noise.
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
        # No such card, or someone else's. Both 404 — distinguishing them would
        # confirm another user's row exists.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Card not found.",
        )

    row = resp.data[0]
    return profit.enrich(row, grading_costs_by_card(client, [row]).get(str(row.get("id"))))
