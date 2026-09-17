"""
backup.py — full snapshot of the Dreamboat Slabs database.

WHY THIS EXISTS (2026-09-17)
    Until today the only copy of 14 months of financial history lived in one
    Supabase project. `pg_dump` had been on the backlog since July and never
    happened. On 2026-09-16 the database was wiped to a clean slate, and on
    9/17 fifty-two rows were deleted — both intentional, both recoverable only
    because a snapshot happened to be taken by hand first. That is luck, not a
    process.

    One bad migration, one DELETE without a WHERE, or one lapsed free-tier
    project and the entire cost basis of a ~$50k inventory is gone. Every other
    problem on this project is fixable. This one isn't.

WHAT IT WRITES
    backups/dreamboat-YYYY-MM-DD-HHMM.json   every row of every table
    backups/cards-YYYY-MM-DD-HHMM.csv        cards only, openable in Excel

    The CSV is deliberate redundancy. If the app, the API, or Python is the
    thing that's broken, a JSON blob is not much comfort — the CSV opens in
    Excel and in Google Sheets, which is where this business ran before and
    where it could run again in an emergency.

    Files land in the VAULT, which OneDrive syncs, so the backup is off the
    machine and off Supabase. The folder is gitignored: these rows contain
    purchase prices, margins and counterparty names, and the repo is public.

IMAGES
    Card photos are downloaded too, into backups/images/, mirroring the storage
    path. `cards.image_url` holds a PATH into a PRIVATE bucket, not a URL, so
    the bytes have to be fetched deliberately — backing up the rows alone would
    restore an inventory of broken image links. Existing files are skipped, so
    a nightly run costs one request per NEW card, not per card.

WHAT IT DOES NOT COVER
    - auth.users. Accounts are re-creatable; a restore would need user_ids
      remapped. The owner's user_id is recorded in the manifest for that reason.
    - Schema. The migrations/ folder is the schema backup and is in git.

RUN
    python backup.py              # write a snapshot
    python backup.py --verify     # write, then re-read and check the row counts
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
KEEP = 30  # snapshots retained; daily runs => about a month of history

# Every table that holds user data. `demo_cards` is fixture data and is
# included anyway because it is cheap and its absence would be confusing.
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

# Columns for the emergency CSV, in an order a human would want them.
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
    """Read a whole table. Returns None if the table does not exist.

    A missing table is NOT an error: `purchase_lots` only exists after 009 and
    `viewer_grants` after 011, so a backup taken against an older database must
    still succeed. It is recorded as missing in the manifest rather than
    silently omitted — a backup that quietly skips a table is worse than one
    that fails, because you only discover the gap during a restore.
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

    # --- card images: bytes, not just paths ---
    img_dir = OUT / "images"
    fetched = skipped = failed = 0
    for row in data.get("cards", []):
        path = row.get("image_url")
        # Legacy rows may hold a full URL rather than a storage path; those are
        # not in our bucket and are skipped rather than guessed at.
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
        except Exception as exc:  # noqa: BLE001 - a bad image must not kill the backup
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

    # --- verify: re-read what was written, don't trust the write ---
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
    # NOTE: backups/images/ is deliberately NOT pruned. Images are content-
    # addressed by card id, they never change, and a pruned image cannot be
    # re-fetched once the card is deleted from Supabase — which is exactly the
    # scenario a backup exists for.
    for pattern in ("dreamboat-*.json", "cards-*.csv"):
        old = sorted(OUT.glob(pattern))[:-KEEP]
        for f in old:
            f.unlink()
        if old:
            print(f"pruned {len(old)} old {pattern}")


if __name__ == "__main__":
    main()
