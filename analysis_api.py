"""Orchestrate an Analysis request without exposing provider keys.

``run_analysis`` is called by the Vercel handler and by the unit tests. Tests
pass a fake transport so nothing here has to reach OpenRouter or Supabase.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import urllib.error
import urllib.request

from analysis_credits import CREDIT_COST, evaluate_spend


ROOT = Path(__file__).resolve().parent
MODELS_PATH = ROOT / "analysis" / "models.json"
DISCLAIMER = (
    "This is not investment advice. It is an automated research draft and it "
    "can be wrong, incomplete, or out of date. Do not use it as the basis for "
    "buying or selling a security."
)

ERROR_MESSAGES = {
    "account_missing": "This account has no credit balance yet.",
    "trial_expired": "The 7-day trial has ended. A paid plan is not available yet.",
    "insufficient_credits": "Not enough credits remain for this request.",
    "invalid_cost": "The credit cost is not valid.",
}


def _env(env: dict, name: str) -> str:
    return (env.get(name) or "").strip()


def analyze_configured(env: dict) -> bool:
    required = ("SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY", "OPENROUTER_API_KEY")
    return all(_env(env, name) for name in required) and MODELS_PATH.exists()


def public_supabase_config(env: dict) -> dict:
    url = _env(env, "SUPABASE_URL")
    anon = _env(env, "SUPABASE_ANON_KEY")
    if not url or not anon:
        return {"configured": False, "supabaseUrl": None, "supabaseAnonKey": None}
    return {"configured": True, "supabaseUrl": url, "supabaseAnonKey": anon}


def load_models() -> dict:
    return json.loads(MODELS_PATH.read_text(encoding="utf-8"))


def estimate_cost_usd(step: dict, prompt_tokens: int, completion_tokens: int) -> float:
    input_rate = float(step.get("input_per_million_usd") or 0)
    output_rate = float(step.get("output_per_million_usd") or 0)
    return (prompt_tokens * input_rate + completion_tokens * output_rate) / 1_000_000


def _bearer(headers: dict) -> str:
    raw = ""
    for key, value in headers.items():
        if key.lower() == "authorization":
            raw = value or ""
            break
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return ""


def _clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[truncated]"


def _documents_block(documents: list) -> str:
    chunks = []
    total = 0
    for item in documents[:20]:
        if not isinstance(item, dict):
            continue
        name = _clip(str(item.get("name") or "upload"), 120)
        body = str(item.get("text") or "")
        room = 80_000 - total
        if room <= 0:
            break
        body = _clip(body, min(12_000, room))
        total += len(body)
        if body.strip():
            chunks.append(f"### {name}\n{body}")
    if not chunks:
        return "No documents were uploaded."
    return (
        "Uploaded documents follow. Give them more weight than the snapshot or general knowledge.\n\n"
        + "\n\n".join(chunks)
    )


class HttpTransport:
    """Network calls used in production. Tests do not use this class."""

    def get_user(self, env: dict, token: str) -> dict | None:
        url = _env(env, "SUPABASE_URL").rstrip("/") + "/auth/v1/user"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "apikey": _env(env, "SUPABASE_ANON_KEY"),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError:
            return None
        if not payload.get("id"):
            return None
        return payload

    def get_account(self, env: dict, user_id: str) -> dict | None:
        url = (
            _env(env, "SUPABASE_URL").rstrip("/")
            + f"/rest/v1/accounts?user_id=eq.{user_id}&select=user_id,credits,tier,trial_ends_at"
        )
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {_env(env, 'SUPABASE_SERVICE_ROLE_KEY')}",
                "apikey": _env(env, "SUPABASE_SERVICE_ROLE_KEY"),
            },
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            rows = json.loads(response.read().decode())
        return rows[0] if rows else None

    def spend(self, env: dict, user_id: str, cost: int, meta: dict) -> int:
        url = _env(env, "SUPABASE_URL").rstrip("/") + "/rest/v1/rpc/spend_credits"
        body = json.dumps(
            {
                "p_user": user_id,
                "p_cost": cost,
                "p_ticker": meta.get("ticker"),
                "p_estimated_cost": meta.get("estimated_cost_usd"),
                "p_actual_cost": meta.get("actual_cost_usd"),
                "p_models": meta.get("models"),
            }
        ).encode()
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {_env(env, 'SUPABASE_SERVICE_ROLE_KEY')}",
                "apikey": _env(env, "SUPABASE_SERVICE_ROLE_KEY"),
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode())
        return int(payload)

    def complete(self, env: dict, model: str, messages: list, max_tokens: int) -> dict:
        body = json.dumps(
            {"model": model, "messages": messages, "max_tokens": max_tokens}
        ).encode()
        request = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {_env(env, 'OPENROUTER_API_KEY')}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=50) as response:
            return json.loads(response.read().decode())


def _message_text(completion: dict) -> str:
    choices = completion.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content") or ""
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
    return str(content)


def _usage(completion: dict) -> tuple[int, int, float | None]:
    usage = completion.get("usage") or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    actual = usage.get("cost")
    if actual is None:
        return prompt, completion_tokens, None
    return prompt, completion_tokens, float(actual)


def run_analysis(env: dict, headers: dict, body: dict, transport) -> tuple[int, dict]:
    if not analyze_configured(env):
        return 503, {
            "configured": False,
            "error": "Analysis requests are not configured. Add the Supabase and OpenRouter environment variables on Vercel.",
        }
    token = _bearer(headers or {})
    if not token:
        return 401, {"configured": True, "error": "Sign in before submitting a request."}
    user = transport.get_user(env, token)
    if not user or not user.get("id"):
        return 401, {"configured": True, "error": "The sign-in token was rejected."}

    models = load_models()
    cost = int(models.get("credit_cost") or CREDIT_COST)
    account = transport.get_account(env, user["id"])
    problem = evaluate_spend(account, cost, datetime.now(timezone.utc))
    if problem:
        return 402, {"configured": True, "error": ERROR_MESSAGES[problem], "code": problem}

    ticker = _clip(str((body or {}).get("ticker") or "SPCX"), 12)
    prompt = _clip(str((body or {}).get("prompt") or ""), 20_000)
    fundamentals = json.dumps((body or {}).get("fundamentals") or {}, default=str)
    fundamentals = _clip(fundamentals, 50_000)
    documents = _documents_block((body or {}).get("documents") or [])
    notes = ""
    estimated = 0.0
    actual_parts: list[float] = []
    actual_known = True
    used_models: list[str] = []

    for step in models["steps"]:
        if step["id"] == "extract":
            messages = [
                {
                    "role": "system",
                    "content": (
                        "Extract only facts that appear in the snapshot or the uploaded documents. "
                        "Mark anything missing as n/a. Do not estimate forward earnings."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Ticker: {ticker}\n\nSnapshot:\n{fundamentals}\n\n{documents}\n\nUser request:\n{prompt}",
                },
            ]
        else:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are writing a research draft. Use the extracted notes. "
                        "Give uploaded documents more weight than the snapshot. "
                        "Do not invent figures. Missing items stay n/a."
                    ),
                },
                {"role": "user", "content": f"User request:\n{prompt}\n\nExtracted notes:\n{notes}"},
            ]
        completion = transport.complete(env, step["model"], messages, int(step.get("max_tokens") or 1200))
        text = _message_text(completion)
        prompt_tokens, completion_tokens, actual = _usage(completion)
        estimated += estimate_cost_usd(step, prompt_tokens, completion_tokens)
        used_models.append(step["model"])
        if actual is None:
            actual_known = False
        else:
            actual_parts.append(actual)
        if step["id"] == "extract":
            notes = text
        else:
            notes = text

    meta = {
        "ticker": ticker,
        "estimated_cost_usd": round(estimated, 6),
        "actual_cost_usd": round(sum(actual_parts), 6) if actual_known else None,
        "models": ", ".join(used_models),
    }
    try:
        remaining = transport.spend(env, user["id"], cost, meta)
        spend_error = None
    except Exception:
        remaining = int(account["credits"])
        spend_error = "The note was generated, but the credit balance could not be updated."
    report = notes.strip()
    if DISCLAIMER not in report:
        report = f"{report}\n\n---\n\n{DISCLAIMER}"
    return 200, {
        "configured": True,
        "markdown": report,
        "disclaimer": DISCLAIMER,
        "estimated_cost_usd": meta["estimated_cost_usd"],
        "actual_cost_usd": meta["actual_cost_usd"],
        "credits_charged": cost,
        "credits_remaining": remaining,
        "spend_error": spend_error,
        "models": used_models,
    }
