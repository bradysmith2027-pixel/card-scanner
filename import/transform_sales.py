"""
transform_sales.py — CardSalesTrackerV3 "Sales" tab -> `cards` rows.

WHAT THIS DOES
    Reads the Sep 11 2026 CSV export, maps it to the post-007/010 `cards`
    schema, and writes two files:

        import/dry-run-cards.json     the rows, ready to POST
        import/reconciliation.md      sheet totals vs. recomputed totals

    It does NOT touch the database. Per [C] Spreadsheet Import Plan.md step 4,
    nothing gets promoted until the reconciliation ties out.

THE DECISIONS BAKED IN HERE (each one is a trap that was checked, not guessed)

    1. "Card Cost" -> purchase_price, with shipping_in = purchase_tax = 0.
       NOT decomposed. Migration 007's header says this explicitly: the sheet's
       rule was "Card Cost is price you paid after shipping & taxes", so the
       number is already all-in. Splitting it would mean inventing two values
       that were never recorded. Historical rows keep the old convention
       honestly rather than being fabricated into the new one.

    2. Sold is detected by `Date Sold`, NEVER by the "Sold Yes or No" column.
       That column reads 1 on 45 cards that have never sold — it is the sheet's
       default, not an assertion. Trusting it would book 45 phantom sales.

    3. Blank fees on a SOLD row are a REAL ZERO. (Corrected 2026-09-17.)
       Brady records fees whenever they were charged, so a blank means none was.
       Verified against the data before accepting it: 13 of 14 eBay sales carry a
       fee, at 10.9-13.5% on large sales rising to 30-63% on sub-$5 cards — which
       is what eBay's ~13% final-value fee plus a fixed per-order fee actually
       does. Discord and cash sales mostly carry none, as expected.

       The rows are still listed below, but as a REVIEW list, not a correction
       list. Exactly one is a true gap: row 72 (Booster Boxes, $341 on eBay) is
       the only eBay sale with no fee recorded, and the fee there is worth $37-46,
       enough to turn that flip from +$41 into a ~$5 LOSS.

    4. Inventory ("what it's worth in hand") -> est_market_value, and ONLY on
       unsold cards. 33 sold cards still carry a stale Inventory number because
       step 6 of the sheet's own instructions (delete it on sale) was skipped.
       Copying those over would overstate holdings by $8,055.

    5. position_type = 'flip' for every row. It is IMMUTABLE in the API, so a
       wrong guess here cannot be corrected later. Nothing in the sheet records
       intent-to-keep, and the hold bucket is 0% for Tranche 1 by decision, so
       'flip' is the only defensible value. Exceptions must be fixed in SQL
       before the API ever sees them.

    6. serial is parsed from the free-text "Card" column (/25, /499, ...) but
       the original text is preserved verbatim in card_type. Parsing is
       additive; nothing is thrown away on the strength of a regex.
"""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).parent
SRC = HERE / "sales-export-2026-09-11.csv"

# Column indices. The export has a TWO-ROW header (row 2 = labels, row 3 =
# sub-labels) and row 2 ALSO holds the column totals, so "$ 14,942.10" is where
# a naive reader finds the header for Card Cost. Indices are pinned on purpose.
C_NUM, C_ACQ, C_YEAR, C_SET, C_PLAYER, C_CARD, C_SPORT = 1, 2, 3, 4, 5, 6, 7
C_BUYCH, C_SELLER, C_COST, C_INV = 8, 9, 11, 12
C_SOLD_DATE, C_BUYER, C_SALECH, C_SALE = 14, 16, 17, 18
C_SHIP_COLL, C_FEES, C_SHIP_OUT = 19, 20, 21

# cards.acquisition_source CHECK (migration 004)
BUY_CHANNEL = {
    "discord": "purchase", "facebook": "purchase", "instagram": "purchase",
    "tiktok": "purchase", "card show": "purchase", "ebay": "purchase",
    "other": "other", "": "other",
}
# cards.sale_channel CHECK (migration 007)
SALE_CHANNEL = {
    "ebay": "ebay", "discord": "discord", "facebook": "facebook",
    "instagram": "instagram", "card show": "show", "show": "show",
    "other": "other",
}


def money(raw: str):
    """'$ 1,234.56' -> Decimal. Returns None for blank, '-' and #DIV/0!.

    The sheet writes an empty cell as ' $ -   ' via its currency format, which
    is a BLANK, not a zero. Treating it as 0 is how a missing fee becomes a
    false claim that no fee was charged.
    """
    s = raw.replace("$", "").replace(",", "").strip()
    if s in ("", "-", "#DIV/0!", "#REF!", "#VALUE!"):
        return None
    try:
        return Decimal(s)
    except Exception:
        return None


def iso(raw: str):
    s = raw.strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_serial(card_text: str):
    """Pull a print run out of free text: 'Auto /25' -> '/25'.

    Stored TEXT AS PRINTED per migration 010 — '/25', not two integers, because
    '1/1', 'FOTL 12/99' and 'A/50' all fail an integer-pair shape. A card with
    no /N is NOT numbered, which is a fact (NULL), not missing data.
    """
    m = re.search(r"/\s*(\d+)", card_text or "")
    return f"/{m.group(1)}" if m else None


# ---------------------------------------------------------------------------
# BRADY'S DECISIONS, 2026-09-17. Recorded here because each one is a judgement
# call that a future reader would otherwise have to re-derive from the data.
#
#   1. Row 64 ("All ebay sales", a batch line) IMPORTS AS-IS. It is real money
#      and excluding it would break tie-out with the sheet. Its ROI is still
#      nonsense; see ROI_EXCLUDE below.
#   2. Missing sale_channel -> 'other'. Missing platform_fees still import as
#      0 (the column is NOT NULL) and remain listed in the backfill table.
#   3. Row 59 sold 2025-06-07 but was acquired 2025-08-01. Brady confirmed a
#      typo. The correction is NOT a guess: rows 57 and 83 both sold
#      2026-06-07, so row 59 was part of that day's batch and the wrong digit
#      is the YEAR. Acquire date is left untouched.
# ---------------------------------------------------------------------------
DATE_FIXES = {
    # sheet row -> (field, corrected value, why)
    "59": ("sale_date", "2026-06-07",
           "sheet read 2025-06-07, two months before acquisition; rows 57 and 83 "
           "sold 2026-06-07, so this was the same batch and the year was mistyped"),
}

# Rows that are real money but are NOT a single card, so their ROI is meaningless.
# Reporting surfaces should exclude these from ROI averages and top-flip rankings.
ROI_EXCLUDE = {"64"}


def main() -> None:
    rows = list(csv.reader(SRC.open(encoding="utf-8-sig")))
    # Row 0-3 are spacer/header/sub-header/spacer. A real row has both a
    # purchase date and a player name; everything else is the template's
    # ~1,900 pre-numbered empty rows.
    raw = [r for r in rows[4:] if len(r) > C_CARD and r[C_ACQ].strip() and r[C_PLAYER].strip()]

    cards, needs_fee_backfill, anomalies, batch_rows, corrections = [], [], [], [], []

    for r in raw:
        num = r[C_NUM].strip()
        sold_date = iso(r[C_SOLD_DATE])
        is_sold = sold_date is not None          # decision 2
        acq_date = iso(r[C_ACQ])
        cost = money(r[C_COST])
        card_text = r[C_CARD].strip()

        card = {
            "_sheet_row": num,
            "player_name": r[C_PLAYER].strip(),
            "year": r[C_YEAR].strip() or None,
            "set_name": r[C_SET].strip() or None,
            "card_type": card_text or None,       # verbatim; decision 6
            "serial": parse_serial(card_text),
            # category is NOT NULL (migration 003 made it required). A blank
            # sport in the sheet becomes 'other' rather than failing the row.
            "category": r[C_SPORT].strip().lower() or "other",
            "purchase_date": acq_date,
            "purchase_price": str(cost) if cost is not None else "0",
            "shipping_in": "0",                   # decision 1 — NOT decomposed
            "purchase_tax": "0",
            "other_costs": "0",
            "acquisition_source": BUY_CHANNEL.get(r[C_BUYCH].strip().lower(), "other"),
            "seller_name": r[C_SELLER].strip() or None,
            "position_type": "flip",              # decision 5 — immutable
            "status": "sold" if is_sold else "in_hand",
        }

        if is_sold:
            fees, ship_out = money(r[C_FEES]), money(r[C_SHIP_OUT])
            card.update({
                "sale_date": sold_date,
                "sale_price": str(money(r[C_SALE]) or 0),
                "shipping_collected": str(money(r[C_SHIP_COLL]) or 0),
                "platform_fees": str(fees or 0),
                "shipping_out": str(ship_out or 0),
                # decision 2 — unknown channel is 'other', never blank
                "sale_channel": SALE_CHANNEL.get(r[C_SALECH].strip().lower(), "other"),
                "buyer_name": r[C_BUYER].strip() or None,
            })
            if fees is None or not r[C_SALECH].strip():
                needs_fee_backfill.append({          # decision 3
                    "row": num, "player": card["player_name"], "sold": sold_date,
                    "sale_price": r[C_SALE].strip(),
                    "missing": [n for n, v in
                                (("platform_fees", fees), ("sale_channel", r[C_SALECH].strip() or None))
                                if not v],
                })
            if num in DATE_FIXES:
                field, value, why = DATE_FIXES[num]
                card[field] = value
                sold_date = value if field == "sale_date" else sold_date
                corrections.append(f"row {num} ({card['player_name']}): {field} -> {value} ({why})")

            if num in ROI_EXCLUDE:
                card["_roi_exclude"] = True

            if acq_date and sold_date < acq_date:
                anomalies.append(f"row {num} ({card['player_name']}): sold {sold_date} BEFORE acquired {acq_date}")
        else:
            inv = money(r[C_INV])                  # decision 4 — unsold only
            if inv is not None:
                card["est_market_value"] = str(inv)

        if cost is None:
            anomalies.append(f"row {num} ({card['player_name']}): no Card Cost — ROI will be undefined (correct: no basis)")

        # A BATCH row is not a card. The sheet has at least one line that lumps
        # many sales together ("All" / "All ebay sales"). It is real money, so
        # dropping it would break tie-out against the sheet's own total — but
        # its ROI is nonsense (1900%) and would distort every average and land
        # at the top of topFlips. Flagged for a human decision, not auto-dropped.
        if card["player_name"].strip().lower() in ("all", "total", "totals", "various", "misc"):
            batch_rows.append({
                "row": num, "text": card["card_type"],
                "cost": card["purchase_price"], "sale": card.get("sale_price"),
            })

        cards.append(card)

    (HERE / "dry-run-cards.json").write_text(json.dumps(cards, indent=2), encoding="utf-8")

    # ---- reconciliation: our math vs. the sheet's own footer totals ----
    def total(key, only_sold=False):
        return sum(Decimal(c.get(key) or 0) for c in cards
                   if not only_sold or c["status"] == "sold")

    sold = [c for c in cards if c["status"] == "sold"]
    cost_all = total("purchase_price")
    cost_sold = sum(Decimal(c["purchase_price"]) for c in sold)
    rev = total("sale_price", True) + total("shipping_collected", True)
    out = total("platform_fees", True) + total("shipping_out", True)
    net = rev - out - cost_sold
    inv_val = sum(Decimal(c["est_market_value"]) for c in cards if c.get("est_market_value"))

    report = f"""# Import reconciliation — Sales tab

Source: `sales-export-2026-09-11.csv` (exported 2026-09-11 21:50)
Generated: {datetime.now():%Y-%m-%d %H:%M}

| | Count |
|---|---|
| Rows imported | **{len(cards)}** |
| Sold | {len(sold)} |
| In hand | {len(cards) - len(sold)} |

| Figure | Sheet footer | Recomputed | Match |
|---|---|---|---|
| Card Cost (all) | $14,942.10 | ${cost_all:,.2f} | {'YES' if cost_all == Decimal('14942.10') else 'NO'} |
| Sale Price | $7,784.46 | ${total('sale_price', True):,.2f} | {'YES' if total('sale_price', True) == Decimal('7784.46') else 'NO'} |
| Selling Fees | $78.85 | ${total('platform_fees', True):,.2f} | {'YES' if total('platform_fees', True) == Decimal('78.85') else 'NO'} |
| Net realized profit | $735.91 | ${net:,.2f} | {'YES' if net == Decimal('735.91') else 'NO'} |
| Inventory valuation | $18,549.66 | ${inv_val:,.2f} | **NO — expected** |

> The Inventory mismatch is the sheet being wrong, not the import. Its footer
> sums the Inventory column across ALL rows, including 33 sold cards that kept a
> stale value because step 6 ("delete your inventory number") was skipped. The
> sheet has been overstating holdings by ~$8,055.

Capital tied up in unsold cards (cost basis): **${cost_all - cost_sold:,.2f}**

## Sold rows with no fee / channel — {len(needs_fee_backfill)} (REVIEW, not a defect list)

🔄 **Corrected 2026-09-17: a blank fee is a real zero.** Brady records fees when
they were charged, and the channel/rate pattern in the data confirms it, so
`platform_fees = 0` is correct on these rows and **$735.91 stands.**

🔴 **The one true gap is row 72** — Booster Boxes, $341 on eBay, the only eBay sale
of 14 with no fee. At his own observed rates that is **$37-46, which flips it from
+$41 to about -$5.** Look the fee up in the eBay record; do not estimate it.

| Row | Player | Sold | Sale price | Missing |
|---|---|---|---|---|
""" + "\n".join(
        f"| {b['row']} | {b['player']} | {b['sold']} | {b['sale_price']} | {', '.join(b['missing'])} |"
        for b in needs_fee_backfill
    ) + "\n\n## Corrections applied during import\n\n" + (
        "\n".join(f"- {c}" for c in corrections) or "- none"
    ) + "\n\n## Batch rows — real money, but NOT a single card\n\n" + (
        "\n".join(
            f"- **row {b['row']}** `{b['text']}` — cost {b['cost']}, sale {b['sale']}. "
            "Imported by Brady's call 9/17 so the totals tie out, and flagged "
            "`_roi_exclude`. Its ROI is meaningless: exclude it from ROI averages "
            "and top-flip rankings or it tops the list on a $5 basis."
            for b in batch_rows
        ) or "- none"
    ) + "\n\n## Anomalies — fix in the sheet, do not guess\n\n" + (
        "\n".join(f"- {a}" for a in anomalies) or "- none"
    ) + "\n"

    (HERE / "reconciliation.md").write_text(report, encoding="utf-8")
    print(f"{len(cards)} cards -> dry-run-cards.json")
    print(f"net profit recomputed ${net:,.2f} vs sheet $735.91 -> {'TIE' if net == Decimal('735.91') else 'MISMATCH'}")
    print(f"{len(needs_fee_backfill)} rows need fee backfill, {len(anomalies)} anomalies")


if __name__ == "__main__":
    main()
