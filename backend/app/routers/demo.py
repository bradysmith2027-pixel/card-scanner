"""
demo.py

GET /demo/cards - the public, read-only inventory behind the portfolio demo.

This is the only endpoint in the app with no login. Someone looking at my
portfolio should be able to see the thing work without me handing out an
account, so this serves a snapshot instead of my real inventory.

Three separate things keep real data out of here, and any one of them would be
enough on its own:

  1. It queries demo_cards, never cards.
  2. It uses the anon key with no user token. RLS on cards only lets you see
     rows where user_id = auth.uid(), and anon has no auth.uid(), so a query
     pointed at the wrong table returns nothing rather than everything.
  3. PUBLIC_FIELDS below is an allow-list. Columns are picked by name, so a
     column added to demo_cards later is not exposed until someone adds it
     here on purpose.

The dollar figures in demo_cards are scaled, not real (see demo_snapshot.py).
The shape of the business is honest; the amounts are not mine.

Profit still runs through app.profit, the same code the real inventory uses.
That is the whole point of the demo: the numbers are computed the correct way,
with fees and shipping and grading taken out.
"""

import logging

from fastapi import APIRouter, HTTPException, Request, status

from app import profit
from app.config import get_settings
from app.rate_limit import limiter

router = APIRouter(prefix="/demo", tags=["demo"])

log = logging.getLogger(__name__)

# Columns the public is allowed to see. Anything not on this list never leaves
# the server, even if it exists on the table.
#
# Deliberately absent: user_id, buyer_name, notes, hold_thesis, ebay_comp_url.
# Those are private or they're strategy. demo_cards shouldn't have them at all
# (migration 012 leaves them off), and this is the second lock on that door.
PUBLIC_FIELDS = (
    "id",
    "player",
    "year",
    "set_name",
    "card_number",
    "serial",
    "category",
    "card_type",
    "status",
    "purchase_price",
    "purchase_date",
    "sale_price",
    "sale_date",
    "shipping_in",
    "purchase_tax",
    "other_costs",
    "shipping_out",
    "platform_fees",
    "shipping_collected",
    "sale_channel",
    "position_type",
    "lane",
    "est_market_value",
    "grading_cost",
    "image_url",
)

# The numbers app.profit adds on. Allowed out alongside PUBLIC_FIELDS.
COMPUTED_FIELDS = (
    "grading_cost",
    "all_in_cost",
    "net_proceeds",
    "net_profit",
    "roi",
    "hold_days",
)

# Plenty for a page load, low enough that the endpoint isn't a free database
# for anyone who finds it.
MAX_ROWS = 500


def _anon_client():
    """
    A Supabase client with the anon key and no user token.

    Not user_client(): that one takes a JWT and there isn't one here. Not
    service_client() either, and that matters - the service role skips RLS, so
    using it on a public endpoint would mean the only thing standing between
    the internet and my real cards is a correct table name.
    """
    from supabase import create_client

    settings = get_settings()
    settings.require("supabase_url", "supabase_anon_key")
    return create_client(settings.supabase_url, settings.supabase_anon_key)


@router.get("/cards")
@limiter.limit("30/minute")
def list_demo_cards(request: Request) -> dict:
    """
    Every card in the public snapshot, with the profit numbers worked out.

    No login. The response looks like GET /cards so the frontend can render it
    with the components it already has.
    """
    try:
        client = _anon_client()
        result = (
            client.table("demo_cards")
            .select(",".join(PUBLIC_FIELDS))
            .order("purchase_date", desc=True)
            .limit(MAX_ROWS)
            .execute()
        )
        rows = result.data or []
    except Exception:
        log.exception("demo cards query failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not load the demo inventory.",
        )

    # profit.enrich_many normally gets grading costs from grading_submissions.
    # There's no demo copy of that table, so the snapshot flattens the total
    # into grading_cost and it gets handed over in the shape enrich expects.
    # Same function, same math, same result as the real inventory.
    submissions_by_card = {
        str(row["id"]): [{"cost": row.get("grading_cost") or 0}] for row in rows
    }

    enriched = profit.enrich_many(rows, submissions_by_card)

    # Filter the response too, not just the query.
    #
    # The select above already asks for named columns, but enrich() copies
    # whatever the row actually contained. That made the database the only
    # thing keeping a private column out of the response - change the select
    # to "*" and everything would leak with nothing to stop it. A test caught
    # this by feeding in a row carrying user_id and buyer_name.
    #
    # Now the allow-list is applied on the way in and on the way out, so both
    # would have to be wrong at once.
    allowed = set(PUBLIC_FIELDS) | set(COMPUTED_FIELDS)
    cards = [{k: v for k, v in card.items() if k in allowed} for card in enriched]

    return {
        "cards": cards,
        "count": len(cards),
        # Said out loud in the payload, not just in the UI, so the numbers
        # can't be mistaken for real ones by anyone reading the API directly.
        "notice": (
            "Read-only demo. Real inventory, real dates and real hold times; "
            "every dollar figure is scaled and is not actual business data."
        ),
    }
