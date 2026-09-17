"""
promote.py — push dry-run-cards.json into the live `cards` table.

RUN THE TRANSFORM FIRST. This reads dry-run-cards.json and writes to Supabase
via PostgREST using the service_role key (RLS is bypassed, so `user_id` must be
stamped explicitly on every row — there is no auth context to infer it from).

SAFETY
    --canary   POST exactly one row, print the server's response, stop.
               Always run this first. A schema mismatch fails on 1 row, not 90.
    --go       POST all rows in batches.
    (default)  validate only; touches nothing.

SCHEMA MISMATCHES THIS FIXES — found by probing the live table, NOT by reading
the migrations, which is why they were caught at all:

    player_name  -> player        The DB column is `player`. `player_name` is the
                                  SCANNER's field name; the frontend has always
                                  translated between them (noted in CLAUDE.md as
                                  a Step 6 reconciliation). A straight POST of
                                  `player_name` 400s on every row.
    seller_name  -> notes         THERE IS NO seller_name COLUMN. The sheet's
                                  Seller is real provenance (66 of 90 cards came
                                  from Discord, most from one seller), so it goes
                                  into notes rather than being dropped.
    _sheet_row   -> notes         Internal. Kept as provenance so any row can be
                                  traced back to the spreadsheet line it came
                                  from — the thing that makes a bad import
                                  fixable instead of permanent.
    _roi_exclude -> stripped      Internal flag, no column. Recorded in the audit
                                  doc instead; row 64 is the only one.
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

# Columns that actually exist on `cards`, probed from the live table.
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
    """Map one dry-run card onto the real `cards` schema."""
    row = {k: v for k, v in card.items() if k in ALLOWED}
    row["user_id"] = user_id
    row["player"] = card["player_name"]              # the rename

    # Provenance. Without the sheet row number a wrong import is unfixable,
    # because nothing ties a DB row back to the line it came from.
    bits = [f"Imported from CardSalesTrackerV3 Sales tab, row {card['_sheet_row']}"]
    if card.get("seller_name"):
        bits.append(f"Seller: {card['seller_name']}")
    if card.get("_roi_exclude"):
        bits.append("BATCH ROW — multiple sales on one line; exclude from ROI averages")
    row["notes"] = ". ".join(bits)

    # Drop nulls so PostgREST applies column defaults rather than writing NULL
    # into a NOT NULL column and failing the whole batch.
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
        # 🔴 PostgREST rejects a bulk insert whose objects have DIFFERENT KEY SETS
        # with PGRST102 "All object keys must match". Nulls are stripped per row
        # (so columns fall back to their defaults instead of writing NULL into a
        # NOT NULL column), which makes every row's key set different — a sold
        # card carries sale fields an unsold one does not.
        #
        # Padding the union with explicit nulls would defeat the stripping and
        # fail on NOT NULL columns. So: group rows by their key signature and
        # send one batch per shape. Same number of rows, a handful of requests.
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
