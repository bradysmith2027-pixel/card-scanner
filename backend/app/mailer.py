"""mailer.py — send a report by email.

Two transports:
  * `send_smtp`  — Gmail SMTP. THE ONE IN USE (chosen 2026-09-29).
  * `send`       — Resend HTTP API. Kept, working and tested, for the day
                   `dreamboatslabs.xyz` comes off clientHold and a proper
                   sending domain exists.

Deliberately tiny. Everything that builds a message is pure, so it can be
tested without a network call or credentials; only `send`/`send_smtp` touch
the wire.

🔴 NOTHING IN HERE MAY LOG MESSAGE CONTENT. The report body carries purchase
prices, margins, buyer names and inventory value. Log subjects, recipients and
status codes — never `text` or `html`.
"""

from __future__ import annotations

import json
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Optional

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 587  # STARTTLS

RESEND_ENDPOINT = "https://api.resend.com/emails"

#: 🔴 LOAD-BEARING. Resend sits behind Cloudflare, which bans urllib's default
#: `Python-urllib/3.x` User-Agent by browser signature. The symptom is a
#: **403 with body `error code: 1010`** — which reads like an auth failure and
#: sends you hunting for a bad API key. It is not: 1010 is a Cloudflare code,
#: not a Resend one. Resend's own errors come back as JSON.
#: Any identifiable UA is accepted. Do not remove this header.
USER_AGENT = "dreamboat-slabs-reports/1.0"

#: Resend's shared sending domain. `dreamboatslabs.xyz` is on clientHold at the
#: registrar, so a custom From domain is not available and deliverability from
#: a bare unverified domain would be poor anyway. Revisit when the domain is back.
DEFAULT_FROM = "Dreamboat Slabs <onboarding@resend.dev>"


class MailError(RuntimeError):
    """Raised when the provider refuses the message.

    This is intentionally loud. A scheduled job that swallows send failures
    produces the single worst outcome available: you stop receiving reports and
    have no idea, because silence is exactly what a healthy quiet week looks
    like too.
    """


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def text_to_html(text: str) -> str:
    """Wrap a plain-text report in minimal, inline-styled HTML.

    Email clients strip <style> blocks and ignore external stylesheets, so the
    styling has to be inline and the layout has to survive being ignored
    entirely. A <pre> block does both: if every style is dropped the report is
    still a readable monospaced column.
    """
    body = _escape(text)
    return (
        '<div style="font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,'
        'monospace;font-size:13px;line-height:1.5;color:#111;">'
        f'<pre style="margin:0;white-space:pre-wrap;">{body}</pre>'
        "</div>"
    )


def _addresses(value: str | list[str] | None) -> list[str]:
    """Normalise one address, a list, or a comma-separated string to a list."""
    if value is None:
        return []
    if isinstance(value, str):
        return [a.strip() for a in value.split(",") if a.strip()]
    return [a.strip() for a in value if a and a.strip()]


def build_payload(
    to: str | list[str],
    subject: str,
    text: str,
    sender: str = DEFAULT_FROM,
    cc: str | list[str] | None = None,
    bcc: str | list[str] | None = None,
) -> dict:
    """Assemble the Resend request body. Pure — no network, no key needed.

    ⚠️ CC vs BCC matters here more than in ordinary mail. This report carries
    purchase prices, margins and buyer names, and a CC shows every recipient's
    address to every other recipient. Use BCC when the readers are not a team
    who already know each other.
    """
    recipients = _addresses(to)
    if not recipients:
        raise ValueError("no recipients")
    if not subject.strip():
        raise ValueError("subject is empty")

    payload = {
        "from": sender,
        "to": recipients,
        "subject": subject,
        "text": text,
        "html": text_to_html(text),
    }
    cc_list = _addresses(cc)
    bcc_list = _addresses(bcc)
    # Omit rather than send empty arrays — some providers treat [] as an error.
    if cc_list:
        payload["cc"] = cc_list
    if bcc_list:
        payload["bcc"] = bcc_list
    return payload


def build_headers(api_key: str) -> dict:
    """Request headers. Pure, so the User-Agent rule can be tested."""
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }


def send(
    api_key: str,
    to: str | list[str],
    subject: str,
    text: str,
    sender: str = DEFAULT_FROM,
    cc: str | list[str] | None = None,
    bcc: str | list[str] | None = None,
    timeout: int = 20,
) -> str:
    """Send one email. Returns the provider's message id.

    Raises MailError on any non-2xx response, with the provider's message but
    WITHOUT the body that failed to send.
    """
    if not api_key:
        raise MailError("RESEND_API_KEY is not set")

    payload = build_payload(to, subject, text, sender, cc=cc, bcc=bcc)
    req = urllib.request.Request(
        RESEND_ENDPOINT,
        data=json.dumps(payload).encode(),
        headers=build_headers(api_key),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode() or "{}")
            return str(body.get("id", ""))
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        raise MailError(f"Resend returned {e.code}: {detail}") from e
    except Exception as e:  # network down, DNS, timeout
        raise MailError(f"Resend request failed: {e}") from e


# ==========================================================================
# Gmail SMTP transport
# ==========================================================================
# Chosen 2026-09-29 over Resend. Resend cannot send from a Gmail address at
# all — it only sends from a DNS-verified domain, and until the account has
# one it may only mail its own signup address and may not CC anyone. Both of
# Brady's domains are on clientHold at Namecheap behind a ticket that has gone
# quiet three times, so "verify a domain" was not a plan with a date on it.
#
# Gmail SMTP needs no domain, sends genuinely FROM dreamboat.slabs@gmail.com,
# allows CC, and Gmail-to-Gmail delivery does not land in spam.
#
# 🔴 Requires an APP PASSWORD, not the account password. Google rejects the
# real password outright. Generate one at myaccount.google.com/apppasswords
# (2-Step Verification must be on first, or the page does not exist).


def build_message(
    sender: str,
    to: str | list[str],
    subject: str,
    text: str,
    cc: str | list[str] | None = None,
    html: Optional[str] = None,
) -> EmailMessage:
    """Build a multipart text+HTML message. Pure — no connection made.

    🔴 BCC IS DELIBERATELY NOT A HEADER HERE. A Bcc: header that reaches the
    wire defeats the entire point of blind copying — every recipient sees the
    list. Blind copies are delivered by naming them in the SMTP envelope
    instead; see `envelope_recipients`.
    """
    recipients = _addresses(to)
    if not recipients:
        raise ValueError("no recipients")
    if not subject.strip():
        raise ValueError("subject is empty")

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    cc_list = _addresses(cc)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain="dreamboatslabs.local")
    # Plain text first, HTML second: in a multipart/alternative the LAST part
    # is what a capable client renders, and the first is what a text-only
    # client (or a screen reader preferring text) falls back to.
    msg.set_content(text)
    msg.add_alternative(html or text_to_html(text), subtype="html")
    return msg


def envelope_recipients(
    to: str | list[str],
    cc: str | list[str] | None = None,
    bcc: str | list[str] | None = None,
) -> list[str]:
    """Everyone the message is actually delivered to, headers aside.

    De-duplicated, because naming the same address twice makes Gmail deliver
    two copies.
    """
    seen: list[str] = []
    for addr in _addresses(to) + _addresses(cc) + _addresses(bcc):
        if addr not in seen:
            seen.append(addr)
    return seen


def send_smtp(
    user: str,
    app_password: str,
    to: str | list[str],
    subject: str,
    text: str,
    cc: str | list[str] | None = None,
    bcc: str | list[str] | None = None,
    sender: Optional[str] = None,
    html: Optional[str] = None,
    timeout: int = 30,
) -> str:
    """Send via Gmail SMTP. Returns the Message-ID.

    Raises MailError on any failure — loudly, because a scheduled job that
    swallows send errors leaves you with no report and no signal, and a
    healthy quiet week looks identical to a broken one.
    """
    if not user:
        raise MailError("GMAIL_USER is not set")
    if not app_password:
        raise MailError(
            "GMAIL_APP_PASSWORD is not set. This must be a Google App Password, "
            "not the account password."
        )

    from_header = sender or f"Dreamboat Slabs <{user}>"
    msg = build_message(from_header, to, subject, text, cc=cc, html=html)
    rcpts = envelope_recipients(to, cc, bcc)

    try:
        with smtplib.SMTP(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, timeout=timeout) as smtp:
            smtp.starttls()
            smtp.login(user, app_password)
            smtp.send_message(msg, from_addr=user, to_addrs=rcpts)
    except smtplib.SMTPAuthenticationError as e:
        raise MailError(
            "Gmail rejected the login. Use an App Password (2-Step Verification "
            f"must be enabled first), not the account password. {e.smtp_code}"
        ) from e
    except Exception as e:
        raise MailError(f"SMTP send failed: {e}") from e

    return str(msg["Message-ID"])
