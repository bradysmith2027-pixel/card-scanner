"""
weekly_report.py — build the Weekly Ops report and email it.

WHY THIS EXISTS (2026-09-28)
    `04 Dreamboat Slabs/[C] Reporting Procedure.md` specifies a weekly review
    and the cadence never happened, because it depended on Brady remembering to
    sit down and write one. A generated report that arrives on its own removes
    the step that was failing. It also enforces the rule the procedure states:
    every figure is computed from the database, never hand-assembled.

HOW IT AUTHENTICATES  — and why not the obvious way
    NOT with the service_role key. The 2026-08-24 security work deliberately
    kept that key off every host; `backup.py` uses it and runs only on Brady's
    laptop for exactly that reason.

    This job signs in as a dedicated REPORT account with password credentials
    and uses the resulting user JWT, so PostgREST applies RLS as it would for
    any user. The account needs a row in `viewer_grants` (migration 011) to see
    the owner's cards — the same read-only mechanism built for the family
    office. It can SELECT and nothing else: migration 011's policies are
    SELECT-only, and Postgres ORs permissive policies per command, so writes
    remain owner-only no matter what this job does.

WHERE IT RUNS
    Railway cron. NOT GitHub Actions: `card-scanner` is a public repo and
    Actions logs on public repos are world-readable, so a traceback containing
    card rows would be published. Railway's logs are private to the account.

🔴 LOGGING RULE
    This script prints counts, status and timings. It NEVER prints report
    content or row data. Anything it logs should be safe on a screen share.

ENV
    SUPABASE_URL                project URL
    SUPABASE_ANON_KEY           public anon key (safe; it is in the frontend bundle)
    REPORT_ACCOUNT_EMAIL        the read-only report account
    REPORT_ACCOUNT_PASSWORD     its password
    REPORT_TO                   where the report is sent (comma-separated)
    REPORT_CC / REPORT_BCC      optional, comma-separated
    GMAIL_USER                  the sending Gmail account
    GMAIL_APP_PASSWORD          a Google APP PASSWORD, not the account password
    REPORT_FROM                 optional From override

RUN
    python weekly_report.py             # build and send
    python weekly_report.py --dry-run   # build and print, send nothing
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
    """Environment first, .env file as a fallback.

    Railway injects real environment variables; the .env file is the local
    convenience. Environment wins so a deployed run can never be silently
    overridden by a stale committed-adjacent file.
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
    """Exchange password credentials for a user JWT."""
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
    if not (dry_run or preview):
        required += ["GMAIL_USER", "GMAIL_APP_PASSWORD", "REPORT_TO"]
    missing = [k for k in required if not env.get(k)]
    if missing:
        # Names only. Never echo a value — printing a secret to prove it is set
        # is exactly how two keys leaked on 2026-09-06.
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
        # An empty result from a read-only account almost always means the
        # viewer_grants row is missing, not that the inventory is empty.
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
        # Written outside the repo: this file contains real inventory and
        # margins, and the repo is public.
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

    # Counts only — never content, and never the addresses themselves.
    print(
        f"sent id={message_id} to={len(recipients)} "
        f"cc={len(cc)} bcc={len(bcc)} "
        f"cards={report.source_counts.get('cards_scanned')} "
        f"purchases={len(report.purchases)} sales={len(report.sales)} "
        f"fee_gaps={len(report.fee_gaps)}"
    )


if __name__ == "__main__":
    main()
