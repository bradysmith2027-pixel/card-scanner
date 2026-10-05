"""
rate_limit.py

The rate limiter for /scan. Every scan calls GPT-4o, which costs money, so I
limit it per user.

The limit goes by the user id in the token, not the IP address. I don't check
the token's signature here because this is only deciding which bucket to count
the request in. current_user still checks the token for real, so a fake token
gets rejected anyway. If there's no token it uses the IP instead.
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


# config_filename points at a file that doesn't exist on purpose. Otherwise
# slowapi tries to read .env on its own, and on Windows it reads it with the
# wrong encoding and crashes. I don't need slowapi's config anyway.
limiter = Limiter(key_func=user_or_ip_key, config_filename="_slowapi_no_env")
