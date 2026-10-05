"""report_html.py

Turns a WeeklyReport into the HTML email.

This is separate from reports.py on purpose. reports.py does the numbers, this
just makes it look nice. Neither one sends the email.

Email HTML is a lot more limited than a web page, so:
  * The layout uses tables. Gmail, Outlook and Apple Mail all handle flexbox
    and grid differently, but tables look the same everywhere.
  * Styles are inline because Gmail removes <style> blocks.
  * No charts or images. Email apps block images by default, so a chart would
    just be an empty box when I check it on my phone. Four big numbers work
    better anyway.
  * 600px wide so it fits without scrolling sideways.

Color never means something on its own. Every colored number also has a +/- or
a word next to it, so it still makes sense in dark mode or printed out.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Optional

from .reports import AGING_THRESHOLDS, CENTS, Line, WeeklyReport, _fmt

# Status colors. Only used for these meanings, not for decoration.
GOOD = "#0ca30c"
WARNING = "#fab219"
CRITICAL = "#d03b3b"

# Warning color for text. The regular WARNING color is too light to read as
# small text on a white background (fine for a filled box though), so this is
# a darker version of it. Don't use WARNING for text.
WARNING_INK = "#7a4f00"

# Background and text colors.
SURFACE = "#fcfcfb"
PLANE = "#f9f9f7"
INK = "#0b0b0b"
INK_2 = "#52514e"
RULE = "#e5e4e0"

FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"


def _h(text: Any) -> str:
    """Escape the text. A set name with a '<' in it would break the rest of the email."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def signed(amount: Optional[Decimal]) -> tuple[str, str]:
    """Returns (text, color). The +/- is in the text so it doesn't rely on color."""
    if amount is None:
        return ("n/a", INK_2)
    q = amount.quantize(CENTS)
    if q > 0:
        return (f"+${q:,.2f}", GOOD)
    if q < 0:
        return (f"-${abs(q):,.2f}", CRITICAL)
    return (f"${q:,.2f}", INK_2)


def _tile(label: str, value: str, color: str = INK, note: str = "") -> str:
    note_html = (
        f'<div style="font:400 12px/1.4 {FONT};color:{INK_2};padding-top:3px;">'
        f"{_h(note)}</div>"
        if note
        else ""
    )
    return (
        f'<td width="25%" style="padding:14px 12px;background:{SURFACE};'
        f'border:1px solid {RULE};border-radius:8px;vertical-align:top;">'
        f'<div style="font:600 11px/1.3 {FONT};color:{INK_2};'
        f'letter-spacing:.06em;text-transform:uppercase;">{_h(label)}</div>'
        f'<div style="font:700 21px/1.25 {MONO};color:{color};padding-top:6px;'
        f'white-space:nowrap;">{_h(value)}</div>{note_html}</td>'
    )


def _rows(lines: list[Line], show_sign: bool = False) -> str:
    if not lines:
        return f'<div style="font:400 14px/1.5 {FONT};color:{INK_2};">None.</div>'
    out = [
        '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="border-collapse:collapse;">'
    ]
    for line in lines:
        money, color = signed(line.amount) if show_sign else (_fmt(line.amount), INK)
        detail = (
            f'<div style="font:400 12px/1.4 {FONT};color:{INK_2};">'
            f"{_h(line.detail)}</div>"
            if line.detail
            else ""
        )
        out.append(
            f'<tr><td style="padding:9px 0;border-bottom:1px solid {RULE};'
            f'font:400 14px/1.4 {FONT};color:{INK};">{_h(line.label)}{detail}</td>'
            f'<td align="right" style="padding:9px 0;border-bottom:1px solid {RULE};'
            f'font:600 14px/1.4 {MONO};color:{color};white-space:nowrap;'
            f'padding-left:12px;">{_h(money)}</td></tr>'
        )
    out.append("</table>")
    return "".join(out)


def _section(title: str, body: str, accent: str = INK) -> str:
    return (
        f'<tr><td style="padding:24px 0 8px 0;">'
        f'<div style="font:700 12px/1.3 {FONT};color:{accent};'
        f'letter-spacing:.07em;text-transform:uppercase;">{_h(title)}</div>'
        f"</td></tr><tr><td>{body}</td></tr>"
    )


def _period(report: WeeklyReport) -> str:
    start, end = report.period_start, report.period_end
    if start.year == end.year:
        return f"{start:%b %d} – {end:%b %d, %Y}"
    return f"{start:%b %d, %Y} – {end:%b %d, %Y}"


def render_html(report: WeeklyReport) -> str:
    """The full HTML email. reports.render_text is the plain text version."""
    net, net_color = signed(report.realized_profit)
    trailing = (
        f"4-wk avg {_fmt(report.prior_4wk_average)}"
        if report.prior_4wk_average is not None
        else ""
    )

    tiles = (
        '<table width="100%" cellpadding="0" cellspacing="6" border="0" '
        'style="border-collapse:separate;"><tr>'
        + _tile("Realized", net, net_color, trailing)
        + _tile(
            "Deployed",
            _fmt(report.purchase_cost),
            INK,
            f"{len(report.purchases)} bought",
        )
        + _tile("Sold", str(len(report.sales)), INK, "closed this week")
        + _tile(
            "In flight",
            _fmt(report.in_transit_dollars),
            INK,
            f"{report.in_transit_count} in transit",
        )
        + "</tr></table>"
    )

    head = [f'<tr><td style="padding-top:16px;">{tiles}</td></tr>']

    if report.quiet:
        head.append(
            f'<tr><td style="padding:16px 0 0 0;font:400 14px/1.6 {FONT};'
            f'color:{INK_2};">No purchases and no sales this week. Sent anyway — '
            "a quiet week and a broken job look identical in an empty inbox."
            "</td></tr>"
        )

    sections: list[str] = []

    # The alert goes first since it's the only part that needs me to do something.
    if report.fee_gaps:
        sections.append(
            f'<tr><td style="padding:24px 0 0 0;">'
            f'<table width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="background:#fdf3f3;border:1px solid {CRITICAL};'
            f'border-radius:8px;"><tr><td style="padding:14px 16px;">'
            f'<div style="font:700 12px/1.3 {FONT};color:{CRITICAL};'
            f'letter-spacing:.07em;text-transform:uppercase;">'
            f"&#9888;&#65039; Action needed &mdash; missing fees</div>"
            f'<div style="font:400 13px/1.5 {FONT};color:{INK};'
            f'padding:6px 0 10px 0;">A fee not captured at close-out is never '
            f"captured. These sold on a channel that always charges one.</div>"
            f"{_rows(report.fee_gaps)}</td></tr></table></td></tr>"
        )

    sections.append(_section("Sales closed", _rows(report.sales, show_sign=True)))
    sections.append(_section("Purchases", _rows(report.purchases)))

    for threshold in AGING_THRESHOLDS:
        lines = report.aging.get(threshold) or []
        if lines:
            sections.append(
                _section(
                    f"Crossed {threshold} days held",
                    _rows(lines),
                    accent=WARNING if threshold == 90 else CRITICAL,
                )
            )

    counts = " &middot; ".join(
        f"{k.replace('_', ' ')}: {v}" for k, v in report.source_counts.items()
    )
    # Only shows up if something is actually missing. If gaps is empty it was
    # a clean week and this section just doesn't show.
    gaps_block = ""
    if report.gaps:
        items = "".join(
            f'<div style="padding-top:5px;">&bull; {_h(g)}</div>'
            for g in report.gaps
        )
        gaps_block = (
            f'<div style="font:400 12px/1.6 {FONT};color:{WARNING_INK};'
            f'padding-top:12px;"><strong>Data gaps</strong>{items}</div>'
        )

    footer = (
        f'<tr><td style="padding:28px 0 0 0;">'
        f'<div style="border-top:1px solid {RULE};padding-top:14px;'
        f'font:400 12px/1.6 {FONT};color:{INK_2};">'
        f"<strong>Built from</strong> &nbsp;{counts}</div>"
        f"{gaps_block}"
        f'<div style="font:400 11px/1.5 {FONT};color:{INK_2};padding-top:14px;">'
        f"Generated from the database. Every figure computed by "
        f"<code>profit.py</code>; nothing typed by hand.</div></td></tr>"
    )

    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Dreamboat Slabs — Weekly Ops</title></head>"
        f'<body style="margin:0;padding:0;background:{PLANE};">'
        f'<table width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{PLANE};padding:24px 12px;">'
        f'<tr><td align="center">'
        f'<table width="600" cellpadding="0" cellspacing="0" border="0" '
        f'style="max-width:600px;width:100%;">'
        f"<tr><td>"
        f'<div style="font:700 20px/1.25 {FONT};color:{INK};">Dreamboat Slabs</div>'
        f'<div style="font:600 12px/1.4 {FONT};color:{INK_2};'
        f'letter-spacing:.08em;text-transform:uppercase;padding-top:3px;">'
        f"Weekly Ops &nbsp;&middot;&nbsp; {_h(_period(report))}</div></td></tr>"
        f"{''.join(head)}{''.join(sections)}{footer}"
        "</table></td></tr></table></body></html>"
    )
