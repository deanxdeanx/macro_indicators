"""Tests for Analysis auth and credit checks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from analysis_api import DISCLAIMER, estimate_cost_usd, run_analysis
from analysis_credits import evaluate_spend, remaining_after_spend


NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)


def account(**overrides):
    base = {
        "user_id": "user-1",
        "credits": 3,
        "tier": "trial",
        "trial_ends_at": (NOW + timedelta(days=3)).isoformat(),
    }
    base.update(overrides)
    return base


class CreditRuleTests(unittest.TestCase):
    def test_trial_can_spend_while_it_has_time_and_credits(self) -> None:
        self.assertIsNone(evaluate_spend(account(), 1, NOW))
        self.assertEqual(remaining_after_spend(account(), 1), 2)

    def test_expired_trial_is_rejected(self) -> None:
        expired = account(trial_ends_at=(NOW - timedelta(days=1)).isoformat())

        self.assertEqual(evaluate_spend(expired, 1, NOW), "trial_expired")

    def test_zero_credits_are_rejected(self) -> None:
        self.assertEqual(evaluate_spend(account(credits=0), 1, NOW), "insufficient_credits")

    def test_missing_account_is_rejected(self) -> None:
        self.assertEqual(evaluate_spend(None, 1, NOW), "account_missing")

    def test_paid_tier_does_not_use_the_trial_clock(self) -> None:
        paid = account(tier="pro", trial_ends_at=(NOW - timedelta(days=30)).isoformat(), credits=2)

        self.assertIsNone(evaluate_spend(paid, 1, NOW))


class FakeTransport:
    def __init__(self, user=None, acct=None, fail_spend=False) -> None:
        self.user = user or {"id": "user-1"}
        self.acct = acct
        self.fail_spend = fail_spend
        self.completions = 0
        self.spent = None

    def get_user(self, env, token):
        if token == "bad":
            return None
        return self.user

    def get_account(self, env, user_id):
        return self.acct

    def spend(self, env, user_id, cost, meta):
        if self.fail_spend:
            raise RuntimeError("rpc down")
        self.spent = (user_id, cost, meta)
        return int(self.acct["credits"]) - cost

    def complete(self, env, model, messages, max_tokens):
        self.completions += 1
        return {
            "choices": [{"message": {"content": f"draft from {model}"}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "cost": 0.001},
        }


CONFIGURED = {
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_ANON_KEY": "anon",
    "SUPABASE_SERVICE_ROLE_KEY": "service",
    "OPENROUTER_API_KEY": "sk-test",
}


class AnalysisRequestTests(unittest.TestCase):
    def test_missing_keys_are_not_configured(self) -> None:
        status, payload = run_analysis({}, {}, {"prompt": "hi"}, FakeTransport())

        self.assertEqual(status, 503)
        self.assertFalse(payload["configured"])
        self.assertNotIn("sk-", payload["error"])

    def test_missing_token_is_unauthorized(self) -> None:
        status, payload = run_analysis(CONFIGURED, {}, {"prompt": "hi"}, FakeTransport())

        self.assertEqual(status, 401)
        self.assertIn("Sign in", payload["error"])

    def test_rejected_token_does_not_call_the_model(self) -> None:
        transport = FakeTransport()

        status, payload = run_analysis(
            CONFIGURED,
            {"Authorization": "Bearer bad"},
            {"prompt": "hi"},
            transport,
        )

        self.assertEqual(status, 401)
        self.assertEqual(transport.completions, 0)
        self.assertIn("rejected", payload["error"])

    def test_insufficient_credits_stop_before_the_model(self) -> None:
        transport = FakeTransport(acct=account(credits=0))

        status, payload = run_analysis(
            CONFIGURED,
            {"Authorization": "Bearer good"},
            {"ticker": "SPCX", "prompt": "Analyze"},
            transport,
        )

        self.assertEqual(status, 402)
        self.assertEqual(payload["code"], "insufficient_credits")
        self.assertEqual(transport.completions, 0)

    def test_successful_run_charges_a_credit_and_adds_the_disclaimer(self) -> None:
        transport = FakeTransport(acct=account())

        status, payload = run_analysis(
            CONFIGURED,
            {"Authorization": "Bearer good"},
            {"ticker": "SPCX", "prompt": "Analyze", "documents": [{"name": "notes.txt", "text": "Revenue grew."}]},
            transport,
        )

        self.assertEqual(status, 200)
        self.assertEqual(transport.completions, 2)
        self.assertEqual(transport.spent[1], 1)
        self.assertEqual(payload["credits_remaining"], 2)
        self.assertIn(DISCLAIMER, payload["markdown"])
        self.assertGreater(payload["estimated_cost_usd"], 0)
        self.assertAlmostEqual(payload["actual_cost_usd"], 0.002)
        self.assertNotIn("OPENROUTER", str(payload))

    def test_estimate_uses_the_configured_rates(self) -> None:
        cost = estimate_cost_usd(
            {"input_per_million_usd": 0.1, "output_per_million_usd": 0.4},
            1_000_000,
            1_000_000,
        )

        self.assertAlmostEqual(cost, 0.5)


if __name__ == "__main__":
    unittest.main()
