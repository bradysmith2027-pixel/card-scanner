"""
demo_snapshot.py

Loads demo_cards from my real inventory, with the dollar figures scaled.

Run this by hand when I want the public demo refreshed. It is not scheduled
and it does not run on Railway. It needs the service role key, which stays on
my laptop, and there is a guard below that stops it if it ever finds itself
running in a deployed environment.

What gets copied:
    Real players, sets, years, card numbers, serials, categories, statuses,
    purchase and sale dates, sale channels, flip vs hold. The shape of the
    business is honest - what I buy, how long I hold it, what sells.

What gets changed:
    Every dollar amount is multiplied by SCALE and then by a per-card jitter.
    The jitter is the same for every money field on a given card, so that
    card's ROI and margin come out exactly right while the absolute numbers
    are not mine. Without the jitter, anyone who knew the real price of one
    card could divide and recover the price of every other card.

What gets dropped:
    user_id, buyer_name, notes, hold_thesis, ebay_comp_url, image_url.
    The first five are private or strategy. image_url is dropped because the
    real images live in a private bucket behind signed URLs that expire;
    publishing those would either break or leak. The demo shows the
    placeholder art instead.

Usage:
    cd backend
    python demo_snapshot.py            # show what it would write, change nothing
    python demo_snapshot.py --apply    # actually replace demo_cards
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from decimal import Decimal, ROUND_HALF_UP

from app.profit import grading_cost_for
from app.supabase_client import service_client

# Every dollar figure gets multiplied by this. 0.6 keeps the numbers in a
# believable range for a card business without being what I actually paid.
SCALE = Decimal("0.60")

# Each card also gets its own multiplier within +/- this much, derived from
# its id so re-running produces the same output. Applied to every money field
# on the card equally, so ROI survives and absolute dollars don't.
JITTER = Decimal("0.12")

MONEY_FIELDS = (
    "purchase_price",
    "sale_price",
    "shipping_in",
    "purchase_tax",
    "other_costs",
    "shipping_out",
    "platform_fees",
    "shipping_collected",
    "est_market_value",
)

# Copied across as-is. Anything not listed here never reaches demo_cards.
PASSTHROUGH_FIELDS = (
    "player",
    "year",
    "set_name",
    "card_number",
    "serial",
    "category",
    "card_type",
    "status",
    "purchase_date",
    "sale_date",
    "sale_channel",
    "position_type",
    "lane",
)

_CENTS = Decimal("0.01")


def _jitter_for(card_id: str) -> Decimal:
    """A stable multiplier in [1-JITTER, 1+JITTER], derived from the card id.

    Hashing the id instead of using random() means running the snapshot twice
    gives the same demo, so the page doesn't change under someone reading it.
    """
    digest = hashlib.sha256(str(card_id).encode()).hexdigest()
    # First 8 hex chars -> 0.0 .. 1.0
    unit = Decimal(int(digest[:8], 16)) / Decimal(0xFFFFFFFF)
    return Decimal(1) - JITTER + (unit * JITTER * 2)


def _scale(value, factor: Decimal):
    """Scale one money value, keeping None as None."""
    if value is None or value == "":
        return None
    try:
        return float(
            (Decimal(str(value)) * SCALE * factor).quantize(_CENTS, rounding=ROUND_HALF_UP)
        )
    except Exception:
        return None


def build_demo_rows(cards, submissions_by_card) -> list[dict]:
    """Turn real card rows into demo rows. Pure function, no database."""
    out = []
    for card in cards:
        cid = str(card.get("id"))
        factor = _jitter_for(cid)

        row = {f: card.get(f) for f in PASSTHROUGH_FIELDS}
        for f in MONEY_FIELDS:
            row[f] = _scale(card.get(f), factor)

        # Flatten the grading submissions into one number the same way the
        # demo endpoint expects to find it.
        gcost = grading_cost_for(submissions_by_card.get(cid))
        row["grading_cost"] = _scale(gcost, factor) or 0

        # Not copied, stated explicitly so it's obvious this was a decision.
        row["image_url"] = None

        out.append(row)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write to demo_cards. Without this it's a dry run.",
    )
    args = parser.parse_args()

    # This script holds the service role key, which skips RLS. It has no
    # business running anywhere but my machine.
    if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"):
        print("Refusing to run in a Railway environment. This is a local-only script.")
        return 1

    client = service_client()

    cards = (client.table("cards").select("*").execute().data) or []
    subs = (
        client.table("grading_submissions").select("card_id,cost").execute().data
    ) or []

    submissions_by_card: dict[str, list] = {}
    for s in subs:
        submissions_by_card.setdefault(str(s.get("card_id")), []).append(s)

    rows = build_demo_rows(cards, submissions_by_card)

    sold = sum(1 for r in rows if (r.get("status") or "").lower() == "sold")
    print(f"real cards read:   {len(cards)}")
    print(f"demo rows built:   {len(rows)}  ({sold} sold)")
    print(f"scale factor:      {SCALE} with +/-{JITTER} per-card jitter")
    print("private fields dropped: user_id, buyer_name, notes, hold_thesis, "
          "ebay_comp_url, image_url")

    if not args.apply:
        print("\nDry run. Nothing written. Re-run with --apply to replace demo_cards.")
        return 0

    # Replace rather than merge, so a card deleted from the real inventory
    # doesn't live on in the demo forever.
    client.table("demo_cards").delete().neq(
        "id", "00000000-0000-0000-0000-000000000000"
    ).execute()
    if rows:
        client.table("demo_cards").insert(rows).execute()

    after = (client.table("demo_cards").select("id").execute().data) or []
    print(f"\nwrote {len(after)} rows to demo_cards.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
