"""
transform_sales.py

Turns the "Sales" tab from my old spreadsheet (CardSalesTrackerV3) into rows
for the cards table.

It reads the CSV I exported, matches it to the cards table, and writes two
files:

    import/dry-run-cards.json     the rows, ready to send
    import/reconciliation.md      the spreadsheet's totals vs what I calculate

It doesn't touch the database. Nothing gets imported until the totals match.

How it handles the spreadsheet:

    1. "Card Cost" -> purchase_price, with shipping_in and purchase_tax = 0.
       In my spreadsheet Card Cost was the price after shipping and tax, so
       it's already the all-in number. I didn't split it up because I'd have to
       make up the shipping and tax amounts.

    2. A card counts as sold if it has a Date Sold, not from the "Sold Yes or
       No" column. That column says 1 on 45 cards that never sold, it's just
       the default. Using it would make 45 fake sales.

    3. A blank fee on a sold card means there was no fee. I always enter fees
       when there is one. I checked the data to make sure: 13 of 14 eBay sales
       have a fee, around 11-13% on bigger sales and higher on cheap cards,
       which is how eBay's fees work. Discord and cash sales mostly don't have
       one.

       The sales with no fee still get listed below, but just to look over.
       Only one is actually missing a fee: row 72 (Booster Boxes, $341 on
       eBay). The fee would be about $37-46, which turns it from +$41 into
       about a $5 loss.

    4. Inventory (what the card is worth) -> est_market_value, but only for
       cards that haven't sold. 33 sold cards still have an old Inventory value
       because I never cleared it when they sold. Copying those would make it
       look like I'm holding $8,055 more than I am.

    5. position_type = 'flip' for every row. It can't be changed later in the
       API, and nothing in the spreadsheet says which cards I meant to keep, so
       flip is the only safe choice. Any exceptions have to be fixed in SQL.

    6. The serial gets pulled out of the "Card" column (/25, /499, ...) but the
       original text is kept in card_type, so nothing gets lost.
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

# Column numbers. The export has a two row header (row 2 = labels, row 3 =
# sub-labels), and row 2 also has the column totals in it, so reading headers
# normally would think "$ 14,942.10" is a header. That's why these are hardcoded.
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

    The spreadsheet shows an empty cell as ' $ -   ' because of the currency
    format. That's blank, not 0.
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
    """Get the print run out of the text: 'Auto /25' -> '/25'.

    Saved as text the way it's printed (migration 010), since serials like
    '1/1', 'FOTL 12/99' and 'A/50' don't fit two numbers. No /N means the card
    isn't numbered.
    """
    m = re.search(r"/\s*(\d+)", card_text or "")
    return f"/{m.group(1)}" if m else None


# ---------------------------------------------------------------------------
# Calls I made on a few specific rows:
#
#   1. Row 64 ("All ebay sales", a bunch of sales on one line) gets imported
#      as is. It's real money and leaving it out would make the totals not
#      match. Its ROI doesn't mean anything though, see ROI_EXCLUDE below.
#   2. No sale_channel -> 'other'. No platform_fees still imports as 0 (the
#      column can't be empty) and stays on the list to check.
#   3. Row 59 says it sold 2025-06-07 but I bought it 2025-08-01. That's a
#      typo. Rows 57 and 83 both sold 2026-06-07, so row 59 was part of that
#      same batch and the year is what's wrong. The buy date stays the same.
# ---------------------------------------------------------------------------
DATE_FIXES = {
    # spreadsheet row -> (field, fixed value, why)
    "59": ("sale_date", "2026-06-07",
           "sheet read 2025-06-07, two months before acquisition; rows 57 and 83 "
           "sold 2026-06-07, so this was the same batch and the year was mistyped"),
}

# Rows that are real money but aren't one card, so their ROI doesn't mean anything.
# Reports should leave these out of ROI averages and top flips.
ROI_EXCLUDE = {"64"}


def main() -> None:
    rows = list(csv.reader(SRC.open(encoding="utf-8-sig")))
    # Rows 0-3 are blank/header/sub-header/blank. A real row has a purchase
    # date and a player. Everything else is the ~1,900 empty rows from the
    # template.
    raw = [r for r in rows[4:] if len(r) > C_CARD and r[C_ACQ].strip() and r[C_PLAYER].strip()]

    cards, needs_fee_backfill, anomalies, batch_rows, corrections = [], [], [], [], []

    for r in raw:
        num = r[C_NUM].strip()
        sold_date = iso(r[C_SOLD_DATE])
        is_sold = sold_date is not None          # rule 2
        acq_date = iso(r[C_ACQ])
        cost = money(r[C_COST])
        card_text = r[C_CARD].strip()

        card = {
            "_sheet_row": num,
            "player_name": r[C_PLAYER].strip(),
            "year": r[C_YEAR].strip() or None,
            "set_name": r[C_SET].strip() or None,
            "card_type": card_text or None,       # kept as is, rule 6
            "serial": parse_serial(card_text),
            # category is required (migration 003). A blank sport becomes
            # 'other' so the row doesn't fail.
            "category": r[C_SPORT].strip().lower() or "other",
            "purchase_date": acq_date,
            "purchase_price": str(cost) if cost is not None else "0",
            "shipping_in": "0",                   # rule 1, not split up
            "purchase_tax": "0",
            "other_costs": "0",
            "acquisition_source": BUY_CHANNEL.get(r[C_BUYCH].strip().lower(), "other"),
            "seller_name": r[C_SELLER].strip() or None,
            "position_type": "flip",              # rule 5, can't change later
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
                # no channel means 'other', never blank
                "sale_channel": SALE_CHANNEL.get(r[C_SALECH].strip().lower(), "other"),
                "buyer_name": r[C_BUYER].strip() or None,
            })
            if fees is None or not r[C_SALECH].strip():
                needs_fee_backfill.append({          # rule 3
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
            inv = money(r[C_INV])                  # rule 4, unsold only
            if inv is not None:
                card["est_market_value"] = str(inv)

        if cost is None:
            anomalies.append(f"row {num} ({card['player_name']}): no Card Cost — ROI will be undefined (correct: no basis)")

        # A row with a bunch of sales lumped together isn't a card ("All" /
        # "All ebay sales"). It's real money so dropping it would throw off the
        # totals, but its ROI is crazy (1900%) and would mess up the averages
        # and top flips. It gets flagged for me to decide, not dropped.
        if card["player_name"].strip().lower() in ("all", "total", "totals", "various", "misc"):
            batch_rows.append({
                "row": num, "text": card["card_type"],
                "cost": card["purchase_price"], "sale": card.get("sale_price"),
            })

        cards.append(card)

    (HERE / "dry-run-cards.json").write_text(json.dumps(cards, indent=2), encoding="utf-8")

    # ---- check my math against the spreadsheet's totals ----
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
