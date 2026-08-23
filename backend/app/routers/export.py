"""
export.py — GET /export/csv: download the user's inventory as a CSV.

The direct replacement for the old Excel workflow. RLS-scoped like everything
else (user_client), so a user only ever exports their own cards. `profit` is a
computed column: sale_price - purchase_price when both are present (i.e. a sold
card), blank otherwise.
"""

import csv
import io
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.auth import AuthedUser, current_user
from app.supabase_client import user_client

router = APIRouter(tags=["export"])

_COLUMNS = [
    "player", "year", "set_name", "card_number", "card_type", "category",
    "purchase_price", "sale_price", "status", "profit", "created_at",
]


def _dec(value) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


@router.get("/export/csv")
def export_csv(user: AuthedUser = Depends(current_user)) -> Response:
    client = user_client(user.token)
    try:
        resp = client.table("cards").select("*").order("created_at", desc=True).execute()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to export cards.",
        )
    rows = resp.data or []

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_COLUMNS)
    for c in rows:
        pp, sp = _dec(c.get("purchase_price")), _dec(c.get("sale_price"))
        profit = str(sp - pp) if (pp is not None and sp is not None) else ""
        writer.writerow([
            c.get("player"), c.get("year"), c.get("set_name"),
            c.get("card_number"), c.get("card_type"), c.get("category"),
            c.get("purchase_price"), c.get("sale_price"), c.get("status"),
            profit, c.get("created_at"),
        ])

    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="dreamboat_slabs_inventory.csv"'
        },
    )
