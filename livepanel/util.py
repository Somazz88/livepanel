"""Small helpers that keep account numbers off the screen and out of logs."""
from __future__ import annotations

import re

_LONG_DIGITS = re.compile(r"\d{6,}")


def mask(acc: object) -> str:
    """'123456789012345678' -> '…5678'. Short ids (IBKR 'U1234567' is 8 chars) keep only the tail too."""
    s = str(acc)
    return "…" + s[-4:] if len(s) > 4 else s


def scrub(text: object, limit: int = 160) -> str:
    """Broker error strings sometimes embed the full account number (moomoo does). Mask any run of 6+ digits."""
    return _LONG_DIGITS.sub(lambda m: "…" + m.group(0)[-4:], str(text))[:limit]
