"""mailer.py

Sends the report emails.

There are three ways to send. weekly_report.py uses Brevo if BREVO_API_KEY is
set, and Gmail SMTP if it's not.
  * send_brevo: Brevo's API. This is what Railway uses, because Railway blocks
                sending email over SMTP unless you're on the Pro plan. More on
                that below.
  * send_smtp:  Gmail SMTP. What my laptop uses. It sends as the actual Gmail
                account so it doesn't end up in spam.
  * send:       Resend's API. Still works, I'm keeping it for when I get my
                dreamboatslabs.xyz domain back.

The parts that build the message don't touch the network, so they can be
tested without any keys. Only the send functions actually send anything.

Never log the email content. The report has purchase prices, margins, buyer
names and inventory value. Logging the subject, who it went to and status
codes is fine, but never the text or html.
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

# Don't remove this. Resend is behind Cloudflare, and Cloudflare blocks the
# default Python-urllib User-Agent. You get a 403 with "error code: 1010",
# which looks like a bad API key but isn't. 1010 is a Cloudflare error, not a
# Resend one (Resend's errors come back as JSON). Any normal User-Agent works.
USER_AGENT = "dreamboat-slabs-reports/1.0"

# Resend's shared sending domain. My dreamboatslabs.xyz domain is on hold right
# now so I can't send from it. Switch this once I have the domain back.
DEFAULT_FROM = "Dreamboat Slabs <onboarding@resend.dev>"


class MailError(RuntimeError):
    """Error for when the email service won't send the message.

    This needs to fail loudly. If the job just ignored send errors, I'd stop
    getting reports and not even know, since no email looks the same as a slow
    week.
    """


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def text_to_html(text: str) -> str:
    """Wrap a plain text report in some basic HTML.

    Email apps remove <style> blocks, so the styles have to be inline. Using a
    <pre> block means even if all the styling gets stripped, it's still
    readable.
    """
    body = _escape(text)
    return (
        '<div style="font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,'
        'monospace;font-size:13px;line-height:1.5;color:#111;">'
        f'<pre style="margin:0;white-space:pre-wrap;">{body}</pre>'
        "</div>"
    )


def _addresses(value: str | list[str] | None) -> list[str]:
    """Turn one address, a list, or a comma separated string into a list."""
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
    """Build the Resend request. Doesn't send anything or need a key.

    CC vs BCC matters here. CC shows everyone's email address to everyone else
    on it. Use BCC if the people getting it don't already know each other.
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
    # Leave these out instead of sending empty lists. Some services error on [].
    if cc_list:
        payload["cc"] = cc_list
    if bcc_list:
        payload["bcc"] = bcc_list
    return payload


def build_headers(api_key: str) -> dict:
    """The request headers. Separate so the User-Agent can be tested."""
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
    """Send one email and return Resend's message id.

    Throws MailError if it doesn't get a 2xx back. The error includes Resend's
    message but not the email body.
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
# Gmail SMTP
# ==========================================================================
# I went with this over Resend. Resend can't send from a Gmail address, only
# from your own verified domain, and until you have one it can only email the
# address you signed up with. Both my domains are on hold at Namecheap right
# now, so that wasn't happening any time soon.
#
# Gmail SMTP doesn't need a domain, sends from dreamboat.slabs@gmail.com, lets
# me CC people, and Gmail to Gmail doesn't go to spam.
#
# This needs an app password, not the normal Gmail password (Google rejects
# that). Make one at myaccount.google.com/apppasswords. 2-Step Verification has
# to be on first or that page won't show up.


def build_message(
    sender: str,
    to: str | list[str],
    subject: str,
    text: str,
    cc: str | list[str] | None = None,
    html: Optional[str] = None,
) -> EmailMessage:
    """Build an email with a text part and an HTML part. Doesn't connect to anything.

    BCC isn't added as a header on purpose. If it was, everyone would see who
    got BCC'd, which defeats the point. BCC people get added when it's sent
    instead (see envelope_recipients).
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
    # Text first, then HTML. Email apps show the last version they can handle,
    # so normal apps show the HTML and text-only ones use the text.
    msg.set_content(text)
    msg.add_alternative(html or text_to_html(text), subtype="html")
    return msg


def envelope_recipients(
    to: str | list[str],
    cc: str | list[str] | None = None,
    bcc: str | list[str] | None = None,
) -> list[str]:
    """Everyone the email actually goes to (to, cc and bcc).

    Duplicates get removed, otherwise Gmail sends the same person two copies.
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
    """Send through Gmail SMTP and return the Message-ID.

    Throws MailError if anything goes wrong. It needs to be loud so I notice,
    since no email looks the same as a slow week.
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


# ==========================================================================
# Brevo
# ==========================================================================
# I added this because the Railway cron job couldn't send with SMTP.
#
# Railway blocks SMTP ports (25/465/587/2525) on plans below Pro, but only
# when the app is running, not while it's building. That's why one test email
# went through (I had the report in the build command) and the next one failed
# with "[Errno 101] Network is unreachable". Regular HTTPS works fine, which is
# how Brevo sends.
#
# Don't try to get around this by running the report in the build command. It
# would only send when the code changes, not on Sundays.
#
# Brevo instead of Resend because Resend needs a verified domain and mine are
# on hold. Brevo just verifies one email address by sending it a code, so
# dreamboat.slabs@gmail.com works without a domain.

BREVO_ENDPOINT = "https://api.brevo.com/v3/smtp/email"


def split_address(value: str) -> dict:
    """"Name <a@b.com>" -> {"name": ..., "email": ...}. Just an address -> email only.

    Brevo wants the name and the address split up, unlike Resend and SMTP.
    """
    value = (value or "").strip()
    if value.endswith(">") and "<" in value:
        name, _, rest = value.rpartition("<")
        return {"email": rest[:-1].strip(), "name": name.strip().strip('"')}
    return {"email": value}


def build_brevo_payload(
    sender: str,
    to: str | list[str],
    subject: str,
    text: str,
    cc: str | list[str] | None = None,
    bcc: str | list[str] | None = None,
    html: Optional[str] = None,
) -> dict:
    """Build the Brevo request. Doesn't send anything or need a key.

    The sender has to be an address I verified in Brevo or it returns a 400
    sender_not_valid.
    """
    recipients = _addresses(to)
    if not recipients:
        raise ValueError("no recipients")
    if not subject.strip():
        raise ValueError("subject is empty")

    payload = {
        "sender": split_address(sender),
        "to": [{"email": a} for a in recipients],
        "subject": subject,
        "textContent": text,
        "htmlContent": html or text_to_html(text),
    }
    # Leave these out instead of sending empty lists. Brevo errors on [].
    cc_list = _addresses(cc)
    bcc_list = _addresses(bcc)
    if cc_list:
        payload["cc"] = [{"email": a} for a in cc_list]
    if bcc_list:
        payload["bcc"] = [{"email": a} for a in bcc_list]
    return payload


def build_brevo_headers(api_key: str) -> dict:
    """The request headers. Separate so the api-key header can be tested.

    Brevo uses an api-key header, not Authorization: Bearer. If you send Bearer
    you get a 401 that says the key is missing, which makes it look like the
    key is wrong when it isn't.
    """
    return {
        "api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }


def send_brevo(
    api_key: str,
    to: str | list[str],
    subject: str,
    text: str,
    sender: str,
    cc: str | list[str] | None = None,
    bcc: str | list[str] | None = None,
    html: Optional[str] = None,
    timeout: int = 20,
) -> str:
    """Send one email through Brevo and return its message id.

    Throws MailError if it doesn't get a 2xx back. The error leaves out the
    email body since it has my prices and margins in it.
    """
    if not api_key:
        raise MailError("BREVO_API_KEY is not set")
    if not sender:
        raise MailError(
            "No sender address. Set REPORT_FROM or GMAIL_USER to the address "
            "verified in Brevo under Senders."
        )

    payload = build_brevo_payload(
        sender, to, subject, text, cc=cc, bcc=bcc, html=html
    )
    req = urllib.request.Request(
        BREVO_ENDPOINT,
        data=json.dumps(payload).encode(),
        headers=build_brevo_headers(api_key),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode() or "{}")
            return str(body.get("messageId", ""))
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        raise MailError(f"Brevo returned {e.code}: {detail}") from e
    except Exception as e:  # network down, DNS, timeout
        raise MailError(f"Brevo request failed: {e}") from e
