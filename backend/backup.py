"""
backup.py

Saves a full copy of the Dreamboat Slabs database.

Before this, the only copy of all my sales history was in Supabase. I'd been
meaning to set up backups since July and never did. Then I cleared out the
database on purpose twice in two days, and both times I only got the data back
because I happened to save a copy by hand first. That was luck.

One bad migration, one DELETE without a WHERE, or my free Supabase project
getting paused, and I'd lose the cost of every card in my inventory. Most
problems on this project can be fixed. That one couldn't.

What it saves:
    backups/dreamboat-YYYY-MM-DD-HHMM.json   every row of every table
    backups/cards-YYYY-MM-DD-HHMM.csv        just the cards, opens in Excel

The CSV is a backup for the backup. If the app or Python is what's broken, a
JSON file doesn't help much, but a CSV opens in Excel or Google Sheets, which
is how I ran the business before.

The files go in my vault, which OneDrive syncs, so they're off my laptop and
off Supabase. The folder is in .gitignore because it has my prices, margins and
buyer names, and the repo is public.

Images:
    Card photos get downloaded to backups/images/, in the same folders as in
    storage. image_url is a path into a private bucket, not a link, so the
    images have to be downloaded on purpose. Otherwise a restore would just
    have broken image links. Images that are already saved get skipped, so
    each night only downloads new ones.

What it doesn't back up:
    - auth.users. Accounts can be made again, but a restore would need the
      user ids swapped. My user id is saved in the manifest for that.
    - The schema. That's what the migrations/ folder is, and it's in git.

Run:
    python backup.py              # save a backup
    python backup.py --verify     # save, then read it back and check the row counts
"""

from __future__ import annotations

import csv
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
ENV = HERE / ".env"
OUT = HERE.parent / "backups"
KEEP = 30  # how many backups to keep, so about a month if it runs every day

# Every table with data in it. demo_cards is just sample data but it's small,
# and it'd be confusing if it was missing.
TABLES = [
    "cards",
    "trades",
    "trade_items",
    "grading_submissions",
    "incoming_shipments",
    "viewer_grants",
    "purchase_lots",
    "demo_cards",
]

# Columns for the CSV, in an order that's easy to read.
CSV_COLS = [
    "player", "year", "set_name", "card_number", "serial", "card_type",
    "category", "status", "position_type", "purchase_date", "purchase_price",
    "shipping_in", "purchase_tax", "other_costs", "acquisition_source",
    "sale_date", "sale_price", "shipping_collected", "platform_fees",
    "shipping_out", "sale_channel", "buyer_name", "est_market_value",
    "notes", "id",
]


def load_env() -> dict:
    env = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k] = v.strip().strip('"').strip("'")
    return env


def fetch(url: str, key: str, table: str):
    """Read a whole table. Returns None if the table doesn't exist.

    A missing table isn't an error. purchase_lots only exists after migration
    009 and viewer_grants after 011, so this still has to work on an older
    database. It gets marked as missing in the manifest though, so I don't
    find out a table was skipped when I'm trying to restore.
    """
    req = urllib.request.Request(
        f"{url}/rest/v1/{table}?select=*",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
    )
    try:
        return json.loads(urllib.request.urlopen(req).read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (404, 400):
            return None
        raise


def main() -> None:
    env = load_env()
    url = env["SUPABASE_URL"].rstrip("/")
    key = env.get("SUPABASE_SERVICE_ROLE_KEY")
    if not key:
        raise SystemExit("SUPABASE_SERVICE_ROLE_KEY is not set; cannot back up.")

    OUT.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")

    data, counts, missing = {}, {}, []
    for t in TABLES:
        rows = fetch(url, key, t)
        if rows is None:
            missing.append(t)
            continue
        data[t] = rows
        counts[t] = len(rows)

    snapshot = {
        "taken_at": datetime.now().isoformat(),
        "supabase_url": url,
        "owner_user_id": "1abe03a8-ae6a-47c7-8b2f-0f3d719f6596",
        "row_counts": counts,
        "tables_not_present": missing,
        "note": (
            "Card images ARE included, under backups/images/. auth.users is "
            "NOT — accounts are re-creatable but user_ids would need remapping, "
            "so owner_user_id is recorded above. Schema lives in "
            "backend/migrations/ under git."
        ),
        "tables": data,
    }

    # --- card images: download the actual files, not just the paths ---
    img_dir = OUT / "images"
    fetched = skipped = failed = 0
    for row in data.get("cards", []):
        path = row.get("image_url")
        # Some old rows have a full link instead of a path. Those aren't in my
        # bucket so they get skipped.
        if not path or "://" in path:
            continue
        dest = img_dir / path
        if dest.exists():
            skipped += 1
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(
            f"{url}/storage/v1/object/card-images/{path}",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
        )
        try:
            dest.write_bytes(urllib.request.urlopen(req).read())
            fetched += 1
        except Exception as exc:  # noqa: BLE001 - one bad image shouldn't stop the whole backup
            failed += 1
            print(f"   image FAILED {path}: {exc}")

    snapshot["images"] = {"fetched": fetched, "already_had": skipped, "failed": failed}

    jpath = OUT / f"dreamboat-{stamp}.json"
    jpath.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")

    cpath = OUT / f"cards-{stamp}.csv"
    with cpath.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLS, extrasaction="ignore")
        w.writeheader()
        for row in data.get("cards", []):
            w.writerow(row)

    print(f"{jpath.name}  ({jpath.stat().st_size:,} bytes)")
    print(f"{cpath.name}  ({len(data.get('cards', []))} cards)")
    print(f"images: {fetched} fetched, {skipped} already saved, {failed} failed")
    for t, n in counts.items():
        print(f"   {t:<22} {n}")
    if missing:
        print(f"   not present: {', '.join(missing)}")

    # --- verify: read the file back and make sure it actually saved ---
    if "--verify" in sys.argv:
        back = json.loads(jpath.read_text(encoding="utf-8"))
        ok = all(len(back["tables"][t]) == n for t, n in counts.items())
        rows = sum(1 for _ in csv.DictReader(cpath.open(encoding="utf-8")))
        ok = ok and rows == counts.get("cards", 0)
        print(f"VERIFY: {'PASS' if ok else 'FAIL'} "
              f"(json tables match, csv {rows} rows)")
        if not ok:
            raise SystemExit(1)

    # --- retention ---
    # backups/images/ doesn't get cleaned up on purpose. The images are saved by
    # card id and never change, and if a card gets deleted from Supabase I
    # can't download its image again. That's the whole reason for a backup.
    for pattern in ("dreamboat-*.json", "cards-*.csv"):
        old = sorted(OUT.glob(pattern))[:-KEEP]
        for f in old:
            f.unlink()
        if old:
            print(f"pruned {len(old)} old {pattern}")


if __name__ == "__main__":
    main()
