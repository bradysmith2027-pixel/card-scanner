"""
cards.py — inventory endpoints backed by the `cards` table.

Every query goes through user_client(token), so Supabase RLS scopes results to
the logged-in user automatically. The backend never relies on a WHERE clause
alone for isolation — the database is the backstop (see the schema design doc).
"""

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.auth import AuthedUser, current_user
from app.supabase_client import user_client

router = APIRouter(prefix="/cards", tags=["cards"])

log = logging.getLogger(__name__)

# Money must be non-negative and within sane bounds (server-side validation,
# per the schema design doc's security requirements). Bad values -> 422.
Money = Annotated[Decimal, Field(ge=0, le=10_000_000)]


class Card(BaseModel):
    """Shape of a row in the `cards` table (mirrors the columns as built)."""

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
    purchase_price: Optional[Money] = None
    purchase_date: Optional[date] = None
    sale_price: Optional[Money] = None
    sale_date: Optional[date] = None
    status: str = "in_hand"
    ebay_comp_url: Optional[str] = None
    image_url: Optional[str] = None
    notes: Optional[str] = None
    acquisition_source: Optional[str] = None


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
        # Generic message to the client; no internal/DB detail leaked.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to fetch cards.",
        )

    return resp.data or []
