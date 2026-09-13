"""
export.py — GET /export/csv: download the user's inventory as a CSV.

The direct replacement for the old Excel workflow. RLS-scoped like everything
else (user_client), so a user only ever exports their own cards.

PROFIT IS NOT COMPUTED HERE (changed 2026-09-13).
    This file used to do `sale_price - purchase_price` inline at line 59 — one
    of four independent profit implementations that had all drifted into being
    wrong. It now imports `app.profit`, the single definition. If profit needs
    to change, it changes there and every surface follows.

    Two bugs died with that line:
      1. It was GROSS — no fees, shipping, tax, or grading.
      2. It computed a profit for ANY card with both prices, including cards
         that were never sold. A card sitting `in_hand` with an aspirational
         sale_price exported a profit number. Only status == 'sold' counts.
"""

import csv
import io
import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.auth import AuthedUser, current_user
from app.profit import enrich_many
from app.routers.cards import grading_costs_by_card
from app.supabase_client import user_client

log = logging.getLogger(__name__)

router = APIRouter(tags=["export"])

# Column order is the reading order of a card's life: what it is, what it cost
# all-in (with the components beside it so a number is never unexplained), what
# it sold for, what came off the top, and what was actually made.
_COLUMNS = [
    "player", "year", "set_name", "card_number", "card_type", "category",
    "position_type", "lane",
    "purchase_price", "shipping_in", "purchase_tax", "other_costs",
    "grading_cost", "all_in_cost",
    "status", "purchase_date", "sale_date", "hold_days",
    "sale_channel", "sale_price", "shipping_collected",
    "platform_fees", "shipping_out", "net_proceeds",
    "net_profit", "roi_pct",
    "est_market_value", "created_at",
]


def _csv_value(value) -> str:
    """Blank for None, so an unsold card's profit cell is EMPTY rather than 0.

    This matters in a spreadsheet: 0 gets averaged, blank does not. An unsold
    card has not made or lost anything, and the export must not imply it broke
    even.
    """
    return "" if value is None else str(value)


def _roi_pct(roi) -> str:
    """ROI as a percentage for humans. Blank when undefined (unsold, or a
    zero-cost pull where return-on-investment has no meaning)."""
    if roi is None:
        return ""
    return f"{roi * 100:.2f}"


@router.get("/export/csv")
def export_csv(user: AuthedUser = Depends(current_user)) -> Response:
    client = user_client(user.token)
    try:
        resp = client.table("cards").select("*").order("created_at", desc=True).execute()
    except Exception:
        # Real cause server-side; generic message to the client.
        log.exception("GET /export/csv failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to export cards.",
        )
    rows = resp.data or []

    # Same enrichment GET /cards uses — the CSV and the UI cannot disagree.
    enriched = enrich_many(rows, grading_costs_by_card(client, rows))

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_COLUMNS)
    for c in enriched:
        writer.writerow([
            _csv_value(c.get("player")),
            _csv_value(c.get("year")),
            _csv_value(c.get("set_name")),
            _csv_value(c.get("card_number")),
            _csv_value(c.get("card_type")),
            _csv_value(c.get("category")),
            _csv_value(c.get("position_type")),
            _csv_value(c.get("lane")),
            _csv_value(c.get("purchase_price")),
            _csv_value(c.get("shipping_in")),
            _csv_value(c.get("purchase_tax")),
            _csv_value(c.get("other_costs")),
            _csv_value(c.get("grading_cost")),
            _csv_value(c.get("all_in_cost")),
            _csv_value(c.get("status")),
            _csv_value(c.get("purchase_date")),
            _csv_value(c.get("sale_date")),
            _csv_value(c.get("hold_days")),
            _csv_value(c.get("sale_channel")),
            _csv_value(c.get("sale_price")),
            _csv_value(c.get("shipping_collected")),
            _csv_value(c.get("platform_fees")),
            _csv_value(c.get("shipping_out")),
            _csv_value(c.get("net_proceeds")),
            _csv_value(c.get("net_profit")),
            _roi_pct(c.get("roi")),
            _csv_value(c.get("est_market_value")),
            _csv_value(c.get("created_at")),
        ])

    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="dreamboat_slabs_inventory.csv"'
        },
    )
