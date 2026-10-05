"""test_mailer.py

Tests for building the emails, and making sure send errors aren't ignored.

No network. build_payload and text_to_html don't send anything, and send is
only tested for its error checks.
"""

import pytest

from app import mailer

pytestmark = pytest.mark.unit


def test_payload_has_both_text_and_html():
    p = mailer.build_payload("a@b.com", "Weekly", "PROFIT $62.00")
    assert p["to"] == ["a@b.com"]
    assert p["subject"] == "Weekly"
    assert p["text"] == "PROFIT $62.00"
    assert "PROFIT $62.00" in p["html"]


def test_single_recipient_is_wrapped_in_a_list():
    assert mailer.build_payload("a@b.com", "s", "t")["to"] == ["a@b.com"]
    assert mailer.build_payload(["a@b.com", "c@d.com"], "s", "t")["to"] == [
        "a@b.com",
        "c@d.com",
    ]


def test_cc_and_bcc_are_included_when_given():
    p = mailer.build_payload(
        "a@b.com", "s", "t", cc="c@d.com", bcc=["e@f.com", "g@h.com"]
    )
    assert p["cc"] == ["c@d.com"]
    assert p["bcc"] == ["e@f.com", "g@h.com"]


def test_cc_and_bcc_are_omitted_when_empty():
    """Some services reject empty lists, so leave those keys out."""
    p = mailer.build_payload("a@b.com", "s", "t")
    assert "cc" not in p
    assert "bcc" not in p
    p = mailer.build_payload("a@b.com", "s", "t", cc="", bcc="   ")
    assert "cc" not in p
    assert "bcc" not in p


def test_comma_separated_addresses_are_split():
    """Env vars come in as one string. Blanks and extra spaces shouldn't turn
    into recipients (a comma at the end would try to send to '')."""
    p = mailer.build_payload("a@b.com, c@d.com,", "s", "t", cc=" e@f.com , ")
    assert p["to"] == ["a@b.com", "c@d.com"]
    assert p["cc"] == ["e@f.com"]


def test_empty_recipients_and_subject_are_refused():
    with pytest.raises(ValueError):
        mailer.build_payload([], "s", "t")
    with pytest.raises(ValueError):
        mailer.build_payload("a@b.com", "   ", "t")


def test_html_escapes_so_a_card_name_cannot_break_the_email():
    """A '<' in a note or set name would break the rest of the email."""
    html = mailer.text_to_html("Panini <Select> & Co")
    assert "&lt;Select&gt;" in html
    assert "&amp;" in html
    assert "<Select>" not in html


def test_html_survives_styles_being_stripped():
    """Email apps drop styling, but a <pre> is still readable."""
    assert "<pre" in mailer.text_to_html("a\nb")


def test_user_agent_is_set_or_cloudflare_403s_us():
    """Makes sure the User-Agent header stays.

    Without it, urllib sends Python-urllib/3.x, Cloudflare blocks it, and
    Resend gives a 403 with "error code: 1010". It looks like a bad API key but
    it isn't. If this fails, expect that 403 to come back.
    """
    headers = mailer.build_headers("test-key")
    assert headers["User-Agent"] == mailer.USER_AGENT
    assert "urllib" not in headers["User-Agent"].lower()
    assert headers["Authorization"] == "Bearer test-key"
    assert headers["Content-Type"] == "application/json"


def test_missing_api_key_raises_rather_than_returning_quietly():
    """If a send fails without an error, I'd just stop getting reports and not know."""
    with pytest.raises(mailer.MailError):
        mailer.send("", "a@b.com", "s", "t")


def test_report_renders_through_the_mailer_unchanged():
    """The report text makes it all the way into the request."""
    from datetime import date

    from app import reports

    r = reports.build_weekly([], today=date(2026, 9, 28))
    text = reports.render_text(r)
    payload = mailer.build_payload("a@b.com", "Weekly", text)
    assert "No purchases and no sales" in payload["text"]
    assert "BUILT FROM" in payload["text"]


# --------------------------------------------------------------------------
# Gmail SMTP
# --------------------------------------------------------------------------
def test_bcc_is_never_a_header_but_is_still_delivered():
    """BCC can't be a header. If it was, everyone could see who got BCC'd."""
    msg = mailer.build_message(
        "Dreamboat <d@gmail.com>", "a@b.com", "s", "t", cc="c@d.com"
    )
    assert msg["Bcc"] is None
    assert msg["To"] == "a@b.com"
    assert msg["Cc"] == "c@d.com"

    # ...but the BCC person is still on the send list, so they still get it.
    rcpts = mailer.envelope_recipients("a@b.com", "c@d.com", "secret@x.com")
    assert rcpts == ["a@b.com", "c@d.com", "secret@x.com"]


def test_envelope_deduplicates_so_nobody_gets_two_copies():
    assert mailer.envelope_recipients("a@b.com", "a@b.com", "a@b.com") == ["a@b.com"]


def test_message_carries_both_plain_text_and_html():
    msg = mailer.build_message("d@gmail.com", "a@b.com", "s", "PROFIT $62.00")
    types = {p.get_content_type() for p in msg.walk()}
    assert "text/plain" in types
    assert "text/html" in types


def test_app_password_guidance_is_in_the_error():
    """Gmail won't take the normal password, so the error should say that."""
    with pytest.raises(mailer.MailError) as e:
        mailer.send_smtp("d@gmail.com", "", "a@b.com", "s", "t")
    assert "App Password" in str(e.value)

    with pytest.raises(mailer.MailError):
        mailer.send_smtp("", "pw", "a@b.com", "s", "t")


def test_smtp_default_sender_uses_the_gmail_account():
    msg = mailer.build_message(
        "Dreamboat Slabs <dreamboat.slabs@gmail.com>", "a@b.com", "s", "t"
    )
    assert "dreamboat.slabs@gmail.com" in msg["From"]


# ==========================================================================
# Brevo
# ==========================================================================


def test_brevo_auth_header_is_api_key_not_bearer():
    """Brevo uses an api-key header, not Bearer.

    If you send Authorization: Bearer, you get a 401 saying the key is missing,
    which makes it look like the key is wrong when it's fine. Same kind of
    thing as the Resend 1010 error above.
    """
    headers = mailer.build_brevo_headers("secret-key")
    assert headers["api-key"] == "secret-key"
    assert "Authorization" not in headers


def test_brevo_splits_display_name_from_address():
    """Brevo wants the name and email split up, unlike SMTP and Resend."""
    assert mailer.split_address("Dreamboat Slabs <d@gmail.com>") == {
        "name": "Dreamboat Slabs",
        "email": "d@gmail.com",
    }
    assert mailer.split_address("d@gmail.com") == {"email": "d@gmail.com"}


def test_brevo_payload_shape():
    payload = mailer.build_brevo_payload(
        "Dreamboat Slabs <d@gmail.com>", "a@b.com", "Weekly", "body",
    )
    assert payload["sender"]["email"] == "d@gmail.com"
    assert payload["to"] == [{"email": "a@b.com"}]
    assert payload["textContent"] == "body"
    assert "<pre" in payload["htmlContent"]


def test_brevo_omits_empty_cc_and_bcc():
    """Brevo errors on [], so these should be left out instead of empty."""
    payload = mailer.build_brevo_payload(
        "d@gmail.com", "a@b.com", "s", "t", cc=None, bcc="",
    )
    assert "cc" not in payload
    assert "bcc" not in payload

    with_cc = mailer.build_brevo_payload(
        "d@gmail.com", "a@b.com", "s", "t", cc="x@y.com,z@y.com",
    )
    assert with_cc["cc"] == [{"email": "x@y.com"}, {"email": "z@y.com"}]


def test_brevo_refuses_to_send_without_key_or_sender():
    """Fail with a clear reason here instead of a 400 from Brevo."""
    with pytest.raises(mailer.MailError) as e:
        mailer.send_brevo("", "a@b.com", "s", "t", "d@gmail.com")
    assert "BREVO_API_KEY" in str(e.value)

    with pytest.raises(mailer.MailError) as e:
        mailer.send_brevo("key", "a@b.com", "s", "t", "")
    assert "Brevo" in str(e.value) or "sender" in str(e.value).lower()


def test_brevo_payload_validates_like_the_other_transports():
    with pytest.raises(ValueError):
        mailer.build_brevo_payload("d@gmail.com", [], "s", "t")
    with pytest.raises(ValueError):
        mailer.build_brevo_payload("d@gmail.com", "a@b.com", "   ", "t")
