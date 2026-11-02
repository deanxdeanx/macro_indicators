"""Credit and trial rules for the Analysis tab.

The SQL function ``spend_credits`` enforces the same checks. These helpers let
the API fail before calling a model, and they are what the unit tests cover.
"""

from __future__ import annotations

from datetime import datetime, timezone


TRIAL_TIER = "trial"
CREDIT_COST = 1


def parse_timestamp(value: datetime | str | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def evaluate_spend(account: dict | None, cost: int, now: datetime) -> str | None:
    """Return an error code, or None when the account can pay ``cost`` credits."""
    if cost < 1:
        return "invalid_cost"
    if not account:
        return "account_missing"
    now_utc = parse_timestamp(now)
    tier = account.get("tier") or TRIAL_TIER
    if tier == TRIAL_TIER:
        trial_ends = parse_timestamp(account.get("trial_ends_at"))
        if trial_ends is None or now_utc is None or trial_ends <= now_utc:
            return "trial_expired"
    try:
        credits = int(account.get("credits"))
    except (TypeError, ValueError):
        return "insufficient_credits"
    if credits < cost:
        return "insufficient_credits"
    return None


def remaining_after_spend(account: dict, cost: int) -> int:
    return int(account["credits"]) - cost
