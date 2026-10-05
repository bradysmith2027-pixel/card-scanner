"""
promote.py

Puts dry-run-cards.json into the real cards table.

Run transform_sales.py first. This reads dry-run-cards.json and saves it to
Supabase with the service_role key. That skips RLS, so user_id has to be set on
every row by hand since there's no logged in user.

Options:
    --canary   send exactly one row, print what comes back, and stop.
               Always do this first so a problem fails on 1 row, not 90.
    --go       send all the rows in batches.
    (default)  just check everything, doesn't save anything.

Things that don't match the real table (I found these by testing against the
actual table, the migrations didn't mention them):

    player_name  -> player        The column is player. player_name is what the
                                  scanner calls it, and the frontend always
                                  switched them. Sending player_name fails on
                                  every row.
    seller_name  -> notes         There's no seller_name column. Who I bought from
                                  still matters (66 of 90 cards came from Discord,
                                  mostly one seller), so it goes in notes.
    _sheet_row   -> notes         Which spreadsheet row it came from, so if
                                  something imports wrong I can trace it back.
    _roi_exclude -> removed       Just a flag, no column for it. Row 64 is the
                                  only one.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
ENV = HERE.parent / "backend" / ".env"
BATCH = 20

# The columns that actually exist on cards, checked against the real table.
ALLOWED = {
    "user_id", "player", "year", "set_name", "card_number", "card_type", "serial",
    "category", "purchase_price", "purchase_date", "shipping_in", "purchase_tax",
    "other_costs", "acquisition_source", "position_type", "status", "sale_price",
    "sale_date", "shipping_collected", "platform_fees", "shipping_out",
    "sale_channel", "buyer_name", "est_market_value", "lane", "hold_thesis",
    "notes", "ebay_comp_url", "image_url", "lot_id",
}


def load_env() -> dict:
    env = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k] = v.strip().strip('"').strip("'")
    return env


def to_row(card: dict, user_id: str) -> dict:
    """Turn one card from the dry run into a row for the real cards table."""
    row = {k: v for k, v in card.items() if k in ALLOWED}
    row["user_id"] = user_id
    row["player"] = card["player_name"]              # rename it

    # Save the spreadsheet row number. Without it there's no way to tell which
    # line a card came from if it imported wrong.
    bits = [f"Imported from CardSalesTrackerV3 Sales tab, row {card['_sheet_row']}"]
    if card.get("seller_name"):
        bits.append(f"Seller: {card['seller_name']}")
    if card.get("_roi_exclude"):
        bits.append("BATCH ROW — multiple sales on one line; exclude from ROI averages")
    row["notes"] = ". ".join(bits)

    # Take out the empty values so the database uses its defaults instead of
    # putting NULL in a required column and failing the whole batch.
    return {k: v for k, v in row.items() if v is not None}


def post(url: str, key: str, rows: list) -> str:
    req = urllib.request.Request(
        url + "/rest/v1/cards",
        data=json.dumps(rows).encode(),
        headers={
            "apikey": key, "Authorization": "Bearer " + key,
            "Content-Type": "application/json", "Prefer": "return=representation",
        },
        method="POST",
    )
    try:
        return urllib.request.urlopen(req).read().decode()
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read().decode()[:600]}"


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "--validate"
    env = load_env()
    url = env["SUPABASE_URL"].rstrip("/")
    key = env["SUPABASE_SERVICE_ROLE_KEY"]
    user_id = env.get("IMPORT_USER_ID", "1abe03a8-ae6a-47c7-8b2f-0f3d719f6596")

    cards = json.loads((HERE / "dry-run-cards.json").read_text(encoding="utf-8"))
    rows = [to_row(c, user_id) for c in cards]

    unknown = {k for c in cards for k in c} - ALLOWED - {"player_name", "seller_name", "_sheet_row", "_roi_exclude"}
    if unknown:
        print(f"STOP — fields with no column: {unknown}")
        return

    print(f"{len(rows)} rows prepared, all fields map to real columns.")
    if mode == "--validate":
        print(json.dumps(rows[0], indent=2))
        print("\nvalidate only. --canary to POST one, --go to POST all.")
        return

    if mode == "--canary":
        print(post(url, key, [rows[0]])[:600])
        return

    if mode == "--go":
        # Supabase rejects a batch if the rows don't all have the same fields
        # (PGRST102 "All object keys must match"). Since empty values get taken
        # out of each row, the rows end up different. A sold card has sale
        # fields and an unsold one doesn't.
        #
        # Filling the missing ones with null would fail on required columns. So
        # rows get grouped by which fields they have, and each group is sent as
        # its own batch.
        groups: dict[tuple, list] = {}
        for r in rows:
            groups.setdefault(tuple(sorted(r)), []).append(r)
        print(f"{len(groups)} distinct column shapes across {len(rows)} rows")

        ok = 0
        for sig, group in groups.items():
            for i in range(0, len(group), BATCH):
                chunk = group[i:i + BATCH]
                res = post(url, key, chunk)
                if res.startswith("HTTP "):
                    print(f"FAILED on shape {len(sig)} cols, rows {i}-{i + len(chunk)}: {res}")
                    print(f"{ok} rows inserted before this failure.")
                    return
                ok += len(chunk)
                print(f"  inserted {ok}/{len(rows)}")
        print(f"DONE — {ok} rows inserted.")


if __name__ == "__main__":
    main()
