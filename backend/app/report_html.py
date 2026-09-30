"""report_html.py — the HTML rendering of a WeeklyReport.

Kept separate from `reports.py` on purpose: that module computes and must stay
pure and easy to test; this one only presents. Neither knows how to send mail.

EMAIL IS NOT THE WEB. The constraints that shaped every choice here:
  * **Layout is tables.** Gmail, Outlook and Apple Mail disagree about flexbox
    and grid. Tables have rendered the same everywhere for twenty years.
  * **Styles are inline.** Gmail strips <style> blocks outright.
  * **No charts, no SVG, no images.** Clients block remote images by default,
    so an image-based chart is an empty box exactly when the report is being
    skimmed on a phone. Per the form heuristic, the honest answer for four
    headline numbers is a stat tile, not a chart.
  * **600px**, the width every client shows without horizontal scroll.

🔴 COLOUR NEVER CARRIES MEANING ALONE. Every coloured figure also has a sign
(+/-) or a word beside it, so the report still reads correctly for a
colourblind reader, in forced dark mode, and on a monochrome printout.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Optional

from .reports import AGING_THRESHOLDS, CENTS, Line, WeeklyReport, _fmt

# Status palette — reserved roles, never reused as decoration.
GOOD = "#0ca30c"
WARNING = "#fab219"
CRITICAL = "#d03b3b"

#: Warning as BODY TEXT. `WARNING` is a mark/fill step — it measures 1.79:1 on
#: the light surface, which is fine for a filled shape next to a label and
#: unreadable as small text. This is the same hue darkened to clear 4.5:1.
#: Never use `WARNING` itself for a run of text.
WARNING_INK = "#7a4f00"

# Surfaces and ink.
SURFACE = "#fcfcfb"
PLANE = "#f9f9f7"
INK = "#0b0b0b"
INK_2 = "#52514e"
RULE = "#e5e4e0"

FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"


def _h(text: Any) -> str:
    """Escape. A set name containing '<' would otherwise eat the rest of the mail."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def signed(amount: Optional[Decimal]) -> tuple[str, str]:
    """(text, colour). The sign lives in the TEXT so colour is never the only cue."""
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
    """The full email document. `reports.render_text` is the plain alternative."""
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

    # The alert is first because it is the only part that asks for an action.
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
    # Only rendered when something is actually wrong. `build_weekly` no longer
    # adds a standing disclaimer, so an empty `gaps` means a clean week and the
    # block disappears entirely rather than printing an empty heading.
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
