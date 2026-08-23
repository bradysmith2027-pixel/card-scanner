"""
rate_limit.py — shared slowapi limiter.

The scan endpoint is the cost-abuse surface (each scan can trigger a paid
GPT-4o call), so it's rate-limited PER USER. The key function pulls the user
id (JWT `sub`) from the bearer token so the limit follows the account, not the
IP. Signature verification is intentionally skipped HERE — this only picks a
bucket key; the endpoint's current_user dependency still fully verifies the
token, so a forged token gets rejected regardless of which bucket it lands in.
Falls back to client IP when there's no usable token.
"""

import jwt
from slowapi import Limiter
from slowapi.util import get_remote_address


def user_or_ip_key(request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            payload = jwt.decode(auth[7:], options={"verify_signature": False})
            sub = payload.get("sub")
            if sub:
                return f"user:{sub}"
        except Exception:
            pass
    return f"ip:{get_remote_address(request)}"


# config_filename points at a deliberately non-existent file: slowapi otherwise
# auto-reads ".env" via starlette Config using the OS default codec (cp1252 on
# Windows), which chokes on our UTF-8 .env. We don't use slowapi's env config,
# and a missing file is simply skipped — so this sidesteps the decode crash.
limiter = Limiter(key_func=user_or_ip_key, config_filename="_slowapi_no_env")
