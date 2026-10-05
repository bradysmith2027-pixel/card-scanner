"""
export.py

GET /export/csv downloads all my cards as a CSV, basically what my old Excel
sheet was. It uses user_client like everything else, so you only get your own
cards.

Profit isn't calculated here. It comes from app.profit like everywhere else.
This file used to just do sale_price - purchase_price, which had two problems:
  1. It didn't take out fees, shipping, tax or grading.
  2. It showed a profit for any card that had both prices, even if it wasn't
     sold yet. Now only cards with status 'sold' get a profit.
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

# The columns go in the order of a card's life: what it is, what it cost (with
# each piece of the cost next to it), what it sold for, what came out of that,
# and what I actually made.
_COLUMNS = [
    # serial is right next to card_number on purpose. They're the two numbers on
    # a card and easy to mix up, so side by side it's easy to see if one is in
    # the wrong column.
    "player", "year", "set_name", "card_number", "serial", "card_type", "category",
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
    """Blank instead of None, so an unsold card's profit cell is empty, not 0.

    In a spreadsheet a 0 gets averaged in and a blank doesn't. An unsold card
    hasn't made or lost anything yet, so it shouldn't look like it broke even.
    """
    return "" if value is None else str(value)


def _roi_pct(roi) -> str:
    """ROI as a normal percentage. Blank if it doesn't apply (not sold yet, or
    a card I pulled that cost me nothing)."""
    if roi is None:
        return ""
    return f"{roi * 100:.2f}"


@router.get("/export/csv")
def export_csv(user: AuthedUser = Depends(current_user)) -> Response:
    client = user_client(user.token)
    try:
        resp = client.table("cards").select("*").order("created_at", desc=True).execute()
    except Exception:
        # Log the real error, send a plain message back.
        log.exception("GET /export/csv failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to export cards.",
        )
    rows = resp.data or []

    # Same numbers GET /cards uses, so the CSV always matches the app.
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
            _csv_value(c.get("serial")),
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
