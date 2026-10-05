"""
weekly_report.py

Builds the Weekly Ops report and emails it.

I planned on doing a weekly review but it never happened, because I had to
remember to sit down and write it. Now it just shows up in my inbox on its
own, and every number comes from the database.

How it logs in:
    It doesn't use the service_role key. That key only lives on my laptop
    (backup.py uses it) and I want to keep it that way.

    Instead it signs in as a separate report account with a password, so RLS
    works like it would for any user. That account has a row in viewer_grants
    (migration 011) so it can see my cards, the same read-only setup my dad's
    account uses. It can only read. The policies in 011 only allow SELECT, so
    it can't change anything.

Where it runs:
    A Railway cron job. Not GitHub Actions, because card-scanner is a public
    repo and anyone can read the Actions logs on a public repo. If it crashed,
    my card data could end up in a public log. Railway's logs are private.

Logging:
    Only prints counts, status and timing. Never the report or any card data.

Env variables:
    SUPABASE_URL                project URL
    SUPABASE_ANON_KEY           public anon key (fine to share, it's in the frontend)
    REPORT_ACCOUNT_EMAIL        the read-only report account
    REPORT_ACCOUNT_PASSWORD     its password
    REPORT_TO                   who gets the report (comma separated)
    REPORT_CC / REPORT_BCC      optional, comma separated
    GMAIL_USER                  the Gmail account it sends from
    GMAIL_APP_PASSWORD          a Google app password, not the normal password
    REPORT_FROM                 optional, to change the From address

Run:
    python weekly_report.py             # build and send
    python weekly_report.py --dry-run   # build and print, don't send
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app import mailer, report_html, reports  # noqa: E402

HERE = Path(__file__).parent
ENV_FILE = HERE / ".env"


def load_env() -> dict:
    """Check the environment first, then the .env file.

    Railway sets real environment variables and .env is just for my laptop.
    The environment wins so an old .env file can't override what's on Railway.
    """
    env: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k] = v.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if v})
    return env


def sign_in(url: str, anon_key: str, email: str, password: str) -> str:
    """Log in with the email and password and get back a token."""
    req = urllib.request.Request(
        f"{url}/auth/v1/token?grant_type=password",
        data=json.dumps({"email": email, "password": password}).encode(),
        headers={"apikey": anon_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            token = json.loads(resp.read().decode()).get("access_token")
    except urllib.error.HTTPError as e:
        raise SystemExit(
            f"Sign-in failed ({e.code}). Check REPORT_ACCOUNT_EMAIL / "
            "REPORT_ACCOUNT_PASSWORD, and that the account is confirmed "
            "(mailer_autoconfirm is false, so it must be confirmed explicitly)."
        ) from e
    if not token:
        raise SystemExit("Sign-in returned no access_token.")
    return token


def fetch(url: str, anon_key: str, token: str, table: str) -> list[dict]:
    req = urllib.request.Request(
        f"{url}/rest/v1/{table}?select=*",
        headers={"apikey": anon_key, "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    preview = "--preview" in sys.argv
    env = load_env()

    required = ["SUPABASE_URL", "SUPABASE_ANON_KEY", "REPORT_ACCOUNT_EMAIL",
                "REPORT_ACCOUNT_PASSWORD"]
    # Picks how to send based on which key is set. My laptop uses Gmail SMTP
    # and Railway uses Brevo, since Railway blocks SMTP unless you're on Pro.
    use_brevo = bool(env.get("BREVO_API_KEY"))
    if not (dry_run or preview):
        required += ["REPORT_TO"]
        required += ["BREVO_API_KEY"] if use_brevo else [
            "GMAIL_USER", "GMAIL_APP_PASSWORD"]
    missing = [k for k in required if not env.get(k)]
    if missing:
        # Only print the names. Never print the values, that's how I leaked two
        # API keys once.
        raise SystemExit(f"Missing env vars: {', '.join(missing)}")

    url = env["SUPABASE_URL"].rstrip("/")
    anon = env["SUPABASE_ANON_KEY"]

    token = sign_in(url, anon, env["REPORT_ACCOUNT_EMAIL"],
                    env["REPORT_ACCOUNT_PASSWORD"])

    cards = fetch(url, anon, token, "cards")
    submissions: dict[str, list[dict]] = {}
    for row in fetch(url, anon, token, "grading_submissions"):
        submissions.setdefault(str(row.get("card_id")), []).append(row)

    if not cards:
        # If the read-only account gets nothing back, it's almost always because
        # the viewer_grants row is missing, not because there are no cards.
        print("WARNING: zero cards visible. Check the viewer_grants row for "
              "this account before trusting an empty report.")

    report = reports.build_weekly(cards, submissions, today=date.today())
    text = reports.render_text(report)
    html = report_html.render_html(report)

    subject = (
        f"Dreamboat Weekly — {report.period_start:%b %d} to "
        f"{report.period_end:%b %d} — {len(report.sales)} sold, "
        f"{len(report.purchases)} bought"
    )

    if preview:
        # Saved outside the repo since it has my real inventory and margins in
        # it, and the repo is public.
        out = Path(os.environ.get("TEMP", ".")) / "dreamboat-weekly-preview.html"
        out.write_text(html, encoding="utf-8")
        print(f"preview written: {out}")
        return

    if dry_run:
        print(text)
        return

    recipients = mailer._addresses(env["REPORT_TO"])
    cc = mailer._addresses(env.get("REPORT_CC"))
    bcc = mailer._addresses(env.get("REPORT_BCC"))
    if use_brevo:
        # The From address has to be verified in Brevo under Senders or it
        # returns 400 sender_not_valid. Brevo verifies single addresses, which
        # is why it works and Resend doesn't while my domains are on hold.
        sender = env.get("REPORT_FROM") or (
            f"Dreamboat Slabs <{env['GMAIL_USER']}>" if env.get("GMAIL_USER")
            else ""
        )
        message_id = mailer.send_brevo(
            env["BREVO_API_KEY"],
            recipients,
            subject,
            text,
            sender,
            cc=cc,
            bcc=bcc,
            html=html,
        )
    else:
        message_id = mailer.send_smtp(
            env["GMAIL_USER"],
            env["GMAIL_APP_PASSWORD"],
            recipients,
            subject,
            text,
            cc=cc,
            bcc=bcc,
            sender=env.get("REPORT_FROM"),
            html=html,
        )

    # Just counts. Never the content or the email addresses.
    print(
        f"sent id={message_id} to={len(recipients)} "
        f"cc={len(cc)} bcc={len(bcc)} "
        f"cards={report.source_counts.get('cards_scanned')} "
        f"purchases={len(report.purchases)} sales={len(report.sales)} "
        f"fee_gaps={len(report.fee_gaps)}"
    )


if __name__ == "__main__":
    main()
