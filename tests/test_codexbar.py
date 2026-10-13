import json
import os
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiusage import codexbar  # noqa: E402

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
UPDATED = "2026-10-12T11:59:30Z"
EMAIL = "person@example.com"
ACCOUNT_ID = "acct-0123456789"

CANONICAL_KEYS = {
    "id", "observed_at", "source", "status", "verified_tier", "auth_method", "auth_verified",
    "account_ref", "windows", "wallet", "error",
}
WINDOW_KEYS = {
    "id", "label", "used_pct", "remaining_pct", "resets_at", "window_minutes", "status",
    "observed_at", "source",
}


class RecordingHasher:
    def __init__(self, value="ref-1"):
        self.value = value
        self.calls = []

    def __call__(self, provider_id, identity):
        self.calls.append((provider_id, dict(identity)))
        return self.value


def win(used, minutes=None, resets=None):
    return {"usedPercent": used, "windowMinutes": minutes, "resetsAt": resets}


def ident(provider, **extra):
    identity = {"providerID": provider, "accountEmail": EMAIL, "accountID": ACCOUNT_ID}
    identity.update(extra)
    return identity


def make_usage(**fields):
    usage = {
        "primary": None,
        "secondary": None,
        "tertiary": None,
        "extraRateWindows": [],
        "updatedAt": UPDATED,
    }
    usage.update(fields)
    return usage


def make_entry(provider, source, usage, **extra):
    entry = {"provider": provider, "source": source, "usage": usage}
    entry.update(extra)
    return entry


def norm(payload, provider, hasher=None):
    return codexbar.normalize(payload, provider, hasher, now=NOW)


def by_id(result):
    return {window["id"]: window for window in result["windows"]}


class NormalizeProviderTests(unittest.TestCase):
    def test_codex_weekly_only_with_null_primary(self):
        hasher = RecordingHasher()
        usage = make_usage(
            secondary=win(43, 10080, "2026-10-18T04:01:44Z"),
            identity=ident("codex", loginMethod="promax"),
        )
        result = norm([make_entry("codex", "oauth", usage)], "codex", hasher)

        self.assertEqual(set(result), CANONICAL_KEYS)
        self.assertEqual(result["id"], "codex")
        self.assertEqual(result["observed_at"], UPDATED)
        self.assertEqual(result["source"], "codexbar:oauth")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["verified_tier"], "promax")
        self.assertEqual(result["auth_method"], "oauth")
        self.assertIs(result["auth_verified"], True)
        self.assertEqual(result["account_ref"], "ref-1")
        self.assertIsNone(result["wallet"])
        self.assertIsNone(result["error"])
        self.assertEqual(
            result["windows"],
            [
                {
                    "id": "secondary-10080m",
                    "label": "Weekly limit",
                    "used_pct": 43.0,
                    "remaining_pct": 57.0,
                    "resets_at": "2026-10-18T04:01:44Z",
                    "window_minutes": 10080,
                    "status": "fresh",
                    "observed_at": UPDATED,
                    "source": "codexbar:oauth",
                }
            ],
        )
        self.assertEqual(
            hasher.calls,
            [("codex", {"accountEmail": EMAIL, "accountID": ACCOUNT_ID, "loginMethod": "promax"})],
        )

    def test_account_ref_is_none_without_hasher(self):
        usage = make_usage(secondary=win(43, 10080), identity=ident("codex", loginMethod="promax"))
        result = norm([make_entry("codex", "oauth", usage)], "codex")
        self.assertIsNone(result["account_ref"])

    def test_cursor_slots_are_not_labelled_weekly(self):
        usage = make_usage(
            primary=win(0.07195767195767196, 43200, "2026-10-22T20:38:49Z"),
            secondary=win(0.07555555555555556, 43200, "2026-10-22T20:38:49Z"),
            tertiary=win(0, 43200, "2026-10-22T20:38:49Z"),
            extraRateWindows=[
                {"kind": "grok_bot", "label": "Grok Bot", "title": "Grok Bot", "window": win(0, 10080)}
            ],
            identity=ident("cursor", loginMethod="Cursor Pro"),
        )
        result = norm([make_entry("cursor", "web", usage)], "cursor")

        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["auth_method"], "session")
        self.assertEqual(result["verified_tier"], "Cursor Pro")
        windows = by_id(result)
        self.assertEqual(
            [windows[key]["label"] for key in ("primary-43200m", "secondary-43200m", "tertiary-43200m")],
            ["Included plan", "Cursor models", "Third-party models"],
        )
        self.assertEqual(windows["primary-43200m"]["used_pct"], 0.07195767195767196)
        self.assertEqual(windows["primary-43200m"]["remaining_pct"], 100.0 - 0.07195767195767196)
        self.assertEqual(windows["tertiary-43200m"]["remaining_pct"], 100.0)
        extra = windows["extra-grok-bot-10080m"]
        self.assertEqual(extra["label"], "Grok Bot")
        self.assertEqual(extra["window_minutes"], 10080)
        for window in result["windows"]:
            self.assertEqual(window["source"], "codexbar:web")

    def test_zai_windows_keep_missing_reset_as_null(self):
        usage = make_usage(
            primary=win(0, 300, None),
            secondary=win(0, 10080, "2026-10-15T10:03:17Z"),
            extraRateWindows=[{"kind": "mcp", "label": "MCP", "window": win(0, 43200)}],
            identity=ident("zai", loginMethod="max"),
        )
        result = norm([make_entry("zai", "api", usage)], "zai")

        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["auth_method"], "api_key")
        self.assertEqual(result["verified_tier"], "max")
        windows = by_id(result)
        self.assertEqual(set(windows), {"primary-300m", "secondary-10080m", "extra-mcp-43200m"})
        primary = windows["primary-300m"]
        self.assertEqual(primary["label"], "5-hour limit")
        self.assertIsNone(primary["resets_at"])
        self.assertEqual((primary["used_pct"], primary["remaining_pct"]), (0.0, 100.0))
        self.assertEqual(windows["secondary-10080m"]["resets_at"], "2026-10-15T10:03:17Z")
        self.assertEqual(windows["extra-mcp-43200m"]["label"], "MCP")

    def test_antigravity_identity_without_windows_is_authenticated_partial(self):
        hasher = RecordingHasher()
        usage = make_usage(identity={"providerID": "antigravity", "accountEmail": EMAIL})
        result = norm([make_entry("antigravity", "oauth", usage, account=None)], "antigravity", hasher)

        self.assertEqual(result["status"], "partial")
        self.assertIs(result["auth_verified"], True)
        self.assertEqual(result["windows"], [])
        self.assertIsNone(result["verified_tier"])
        self.assertIsNone(result["error"])
        self.assertEqual(result["account_ref"], "ref-1")
        self.assertEqual(hasher.calls, [("antigravity", {"accountEmail": EMAIL})])

    def test_no_identity_and_no_windows_is_unavailable(self):
        result = norm([make_entry("codex", "oauth", make_usage())], "codex")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["error"]["code"], "no_quota_data")
        self.assertIs(result["auth_verified"], False)
        self.assertEqual(result["windows"], [])
        self.assertIsNone(result["account_ref"])

    def test_antigravity_availability_pattern_is_not_quota(self):
        usage = make_usage(
            primary=win(0), secondary=win(0), identity={"providerID": "antigravity", "accountEmail": EMAIL}
        )
        result = norm([make_entry("antigravity", "oauth", usage)], "antigravity")
        self.assertEqual(result["status"], "partial")
        for window in result["windows"]:
            self.assertEqual(window["status"], "unknown")
            self.assertIsNone(window["used_pct"])
            self.assertIsNone(window["remaining_pct"])

    def test_antigravity_quota_with_duration_and_reset_is_kept(self):
        usage = make_usage(
            primary=win(0, 300, "2026-10-12T16:00:00Z"),
            identity={"providerID": "antigravity", "accountEmail": EMAIL},
        )
        result = norm([make_entry("antigravity", "oauth", usage)], "antigravity")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["windows"][0]["remaining_pct"], 100.0)

    def test_zero_usage_without_metadata_is_kept_for_other_providers(self):
        usage = make_usage(primary=win(0), identity=ident("codex"))
        result = norm([make_entry("codex", "oauth", usage)], "codex")
        self.assertEqual(result["windows"][0]["status"], "fresh")
        self.assertEqual(result["windows"][0]["remaining_pct"], 100.0)


class NormalizeWindowTests(unittest.TestCase):
    def test_zero_hundred_and_overage(self):
        usage = make_usage(
            primary=win(0, 300),
            secondary=win(100, 10080),
            tertiary=win(137.5, 43200),
            identity=ident("codex"),
        )
        windows = by_id(norm([make_entry("codex", "oauth", usage)], "codex"))
        self.assertEqual((windows["primary-300m"]["used_pct"], windows["primary-300m"]["remaining_pct"]), (0.0, 100.0))
        self.assertEqual(
            (windows["secondary-10080m"]["used_pct"], windows["secondary-10080m"]["remaining_pct"]), (100.0, 0.0)
        )
        overage = windows["tertiary-43200m"]
        self.assertEqual((overage["used_pct"], overage["remaining_pct"]), (137.5, 0.0))
        self.assertEqual(overage["status"], "fresh")
        for window in windows.values():
            self.assertIsInstance(window["used_pct"], float)

    def test_malformed_used_percent_becomes_unknown_not_zero(self):
        bad_values = [True, False, -1, -0.5, float("nan"), float("inf"), "12", None, [], {}]
        for bad in bad_values:
            with self.subTest(bad=repr(bad)):
                usage = make_usage(secondary=win(bad, 10080), identity=ident("codex"))
                result = norm([make_entry("codex", "oauth", usage)], "codex")
                window = result["windows"][0]
                self.assertEqual(window["status"], "unknown")
                self.assertIsNone(window["used_pct"])
                self.assertIsNone(window["remaining_pct"])
                self.assertEqual(result["status"], "partial")
                self.assertIs(result["auth_verified"], True)

    def test_missing_used_percent_key_is_unknown(self):
        usage = make_usage(secondary={"windowMinutes": 10080}, identity=ident("codex"))
        result = norm([make_entry("codex", "oauth", usage)], "codex")
        self.assertEqual(result["windows"][0]["status"], "unknown")
        self.assertEqual(result["status"], "partial")

    def test_non_dict_slot_is_unknown_and_good_windows_survive(self):
        usage = make_usage(primary="oops", secondary=win(10, 10080), identity=ident("codex"))
        result = norm([make_entry("codex", "oauth", usage)], "codex")
        windows = by_id(result)
        self.assertEqual(windows["primary"]["status"], "unknown")
        self.assertEqual(windows["secondary-10080m"]["status"], "fresh")
        self.assertEqual(result["status"], "partial")

    def test_absent_slots_are_not_zero_usage(self):
        usage = make_usage(secondary=win(43, 10080), identity=ident("codex"))
        result = norm([make_entry("codex", "oauth", usage)], "codex")
        self.assertEqual([window["id"] for window in result["windows"]], ["secondary-10080m"])

    def test_window_minutes_validation(self):
        cases = [
            (True, None), (0, None), (-5, None), (1.5, None), ("300", None),
            (float("nan"), None), (10**9, None), (300, 300), (300.0, 300),
        ]
        for raw, expected in cases:
            with self.subTest(raw=repr(raw)):
                usage = make_usage(secondary=win(10, raw), identity=ident("codex"))
                window = norm([make_entry("codex", "oauth", usage)], "codex")["windows"][0]
                self.assertEqual(window["window_minutes"], expected)
                self.assertEqual(window["id"], "secondary" if expected is None else f"secondary-{expected}m")

    def test_reset_timestamp_validation(self):
        cases = [
            ("garbage", None),
            ("2026-10-18 04:01:44", None),
            (12345, None),
            (None, None),
            ("2026-13-40T00:00:00Z", None),
            ("2026-10-18T04:01:44Z", "2026-10-18T04:01:44Z"),
            ("2026-10-18T06:01:44+02:00", "2026-10-18T04:01:44Z"),
            ("2026-10-18T04:01:44.5Z", "2026-10-18T04:01:44.500000Z"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=repr(raw)):
                usage = make_usage(secondary=win(10, 10080, raw), identity=ident("codex"))
                window = norm([make_entry("codex", "oauth", usage)], "codex")["windows"][0]
                self.assertEqual(window["resets_at"], expected)
                self.assertEqual(window["status"], "fresh")

    def test_stale_timestamps_are_preserved(self):
        usage = make_usage(
            updatedAt="2026-01-01T00:00:00Z",
            secondary=win(10, 10080, "2026-01-02T00:00:00Z"),
            identity=ident("codex"),
        )
        result = norm([make_entry("codex", "oauth", usage)], "codex")
        self.assertEqual(result["observed_at"], "2026-01-01T00:00:00Z")
        window = result["windows"][0]
        self.assertEqual(window["observed_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(window["resets_at"], "2026-01-02T00:00:00Z")
        self.assertEqual(window["status"], "fresh")

    def test_invalid_or_future_updated_at_falls_back_to_now(self):
        for raw in ("nope", None, 5, "2026-10-12T13:00:00Z"):
            with self.subTest(raw=repr(raw)):
                usage = make_usage(updatedAt=raw, secondary=win(10, 10080), identity=ident("codex"))
                result = norm([make_entry("codex", "oauth", usage)], "codex")
                self.assertEqual(result["observed_at"], "2026-10-12T12:00:00Z")
                self.assertEqual(result["windows"][0]["observed_at"], "2026-10-12T12:00:00Z")

    def test_unlimited_is_not_one_hundred_percent(self):
        unlimited = {"usedPercent": None, "windowMinutes": 300, "resetsAt": None, "unlimited": True}
        usage = make_usage(primary=unlimited, identity=ident("codex"))
        result = norm([make_entry("codex", "oauth", usage)], "codex")
        window = result["windows"][0]
        self.assertEqual(window["status"], "unlimited")
        self.assertIsNone(window["used_pct"])
        self.assertIsNone(window["remaining_pct"])
        self.assertEqual(result["status"], "verified")

    def test_low_confidence_or_unavailable_markers_become_unknown(self):
        cases = {
            "usage-confidence": make_usage(
                secondary=win(10, 10080), dataConfidence="low", identity=ident("codex")
            ),
            "usage-unavailable": make_usage(
                secondary=win(10, 10080), unavailable=True, identity=ident("codex")
            ),
            "window-unavailable": make_usage(
                secondary={"usedPercent": 10, "windowMinutes": 10080, "unavailable": True},
                identity=ident("codex"),
            ),
            "window-confidence": make_usage(
                secondary={"usedPercent": 10, "windowMinutes": 10080, "dataConfidence": 3},
                identity=ident("codex"),
            ),
        }
        for name, usage in cases.items():
            with self.subTest(name):
                result = norm([make_entry("codex", "oauth", usage)], "codex")
                window = result["windows"][0]
                self.assertEqual(window["status"], "unknown")
                self.assertIsNone(window["used_pct"])
                self.assertIsNone(window["remaining_pct"])
                self.assertEqual(result["status"], "partial")

    def test_verified_confidence_marker_keeps_data(self):
        usage = make_usage(secondary=win(10, 10080), dataConfidence="verified", identity=ident("codex"))
        result = norm([make_entry("codex", "oauth", usage)], "codex")
        self.assertEqual(result["windows"][0]["status"], "fresh")
        self.assertEqual(result["status"], "verified")

    def test_degraded_data_without_identity_is_unavailable(self):
        usage = make_usage(secondary=win(10, 10080), dataConfidence="low")
        result = norm([make_entry("codex", "oauth", usage)], "codex")
        self.assertEqual(result["status"], "unavailable")
        self.assertIs(result["auth_verified"], False)
        self.assertEqual(result["windows"], [])


class NormalizeExtraWindowTests(unittest.TestCase):
    @staticmethod
    def extra(kind, label, title, minutes=43200, used=1):
        return {"kind": kind, "label": label, "title": title, "window": win(used, minutes)}

    def normalize_extras(self, items):
        usage = make_usage(extraRateWindows=items, identity=ident("cursor"))
        return norm([make_entry("cursor", "web", usage)], "cursor")

    def test_colliding_ids_are_disambiguated_independent_of_order(self):
        items = [self.extra("model", "Alpha model", "Alpha"), self.extra("model", "Beta model", "Beta")]
        forward = {w["label"]: w["id"] for w in self.normalize_extras(items)["windows"]}
        backward = {w["label"]: w["id"] for w in self.normalize_extras(list(reversed(items)))["windows"]}
        self.assertEqual(forward, backward)
        self.assertEqual(len(set(forward.values())), 2)
        for window_id in forward.values():
            self.assertRegex(window_id, r"^extra-model-43200m-[0-9a-f]{6}$")

    def test_identical_descriptors_still_get_unique_ids(self):
        items = [self.extra("model", "Same", "Same"), self.extra("model", "Same", "Same")]
        ids = [w["id"] for w in self.normalize_extras(items)["windows"]]
        self.assertEqual(len(set(ids)), 2)
        self.assertTrue(ids[1].endswith("-2"))

    def test_non_ascii_labels_are_stable_and_distinct(self):
        items = [self.extra(None, "月度额度", None), self.extra(None, "周额度", None)]
        windows = self.normalize_extras(items)["windows"]
        self.assertEqual({w["label"] for w in windows}, {"月度额度", "周额度"})
        self.assertEqual(len({w["id"] for w in windows}), 2)
        for window in windows:
            self.assertRegex(window["id"], r"^extra-window-43200m-[0-9a-f]{6}$")

    def test_unique_extra_id_has_no_hash_suffix(self):
        windows = self.normalize_extras([self.extra("grok_bot", "Grok Bot", None, minutes=10080)])["windows"]
        self.assertEqual(windows[0]["id"], "extra-grok-bot-10080m")

    def test_extra_without_window_is_unknown(self):
        result = self.normalize_extras(
            [{"kind": "x", "label": "X", "window": None}, self.extra("ok", "OK", None)]
        )
        windows = by_id(result)
        self.assertEqual(windows["extra-x"]["status"], "unknown")
        self.assertEqual(windows["extra-ok-43200m"]["status"], "fresh")
        self.assertEqual(result["status"], "partial")

    def test_malformed_extras_make_result_partial(self):
        for extras in (["junk"], "junk", {"kind": "x"}):
            with self.subTest(extras=repr(extras)):
                usage = make_usage(secondary=win(10, 10080), extraRateWindows=extras, identity=ident("codex"))
                result = norm([make_entry("codex", "oauth", usage)], "codex")
                self.assertEqual(result["status"], "partial")
                self.assertEqual(len(result["windows"]), 1)

    def test_extra_ids_never_collide_with_slot_ids(self):
        usage = make_usage(
            primary=win(1, 300),
            extraRateWindows=[self.extra("primary", "Primary", None, minutes=300)],
            identity=ident("codex"),
        )
        result = norm([make_entry("codex", "oauth", usage)], "codex")
        ids = [w["id"] for w in result["windows"]]
        self.assertEqual(len(ids), len(set(ids)))


class NormalizeWalletTests(unittest.TestCase):
    def wallet(self, cost):
        usage = make_usage(secondary=win(1, 10080), providerCost=cost, identity=ident("cursor"))
        return norm([make_entry("cursor", "web", usage)], "cursor")["wallet"]

    def test_wallet_formats(self):
        cases = [
            ({"balance": 12.5, "currencyCode": "USD"}, "12.5", "USD"),
            ({"balance": 15, "limit": 20, "used": 5, "currencyCode": "USD"}, "15", "USD"),
            ({"balance": 0, "currencyCode": "USD"}, "0", "USD"),
            ({"balance": 1e-7, "currencyCode": "USD"}, "0.0000001", "USD"),
            ({"balance": 100.0, "currencyCode": "USD"}, "100", "USD"),
        ]
        for cost, balance, unit in cases:
            with self.subTest(cost=cost):
                self.assertEqual(self.wallet(cost), {"balance": balance, "unit": unit, "label": "Balance"})

    def test_credits_unit_is_not_coerced_to_usd(self):
        wallet = self.wallet({"balance": 250, "currencyCode": "Credits"})
        self.assertEqual(wallet["unit"], "Credits")
        self.assertNotEqual(wallet["unit"], "USD")

    def test_invalid_wallet_fields_are_dropped(self):
        for balance in (float("nan"), float("inf"), float("-inf"), -1, True, "5", None):
            with self.subTest(balance=repr(balance)):
                self.assertIsNone(self.wallet({"balance": balance, "currencyCode": "USD"}))
        for code in (None, "", "US$", 5, "x" * 40):
            with self.subTest(code=repr(code)):
                self.assertIsNone(self.wallet({"balance": 5, "currencyCode": code}))
        self.assertIsNone(self.wallet({"limit": 20, "used": 5, "currencyCode": "USD"}))
        self.assertIsNone(self.wallet("junk"))

    def test_wallet_does_not_create_windows(self):
        usage = make_usage(providerCost={"balance": 5, "currencyCode": "USD"})
        result = norm([make_entry("cursor", "web", usage)], "cursor")
        self.assertEqual(result["windows"], [])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["wallet"]["balance"], "5")


class NormalizePlanTests(unittest.TestCase):
    def tier(self, usage, **entry_extra):
        usage["secondary"] = win(1, 10080)
        return norm([make_entry("codex", "oauth", usage, **entry_extra)], "codex")["verified_tier"]

    def test_plan_sources_and_precedence(self):
        self.assertEqual(self.tier(make_usage(identity=ident("codex", loginMethod="Max"), loginMethod="Pro")), "Max")
        self.assertEqual(self.tier(make_usage(loginMethod="Pro")), "Pro")
        self.assertEqual(self.tier(make_usage(plan="Plus")), "Plus")
        self.assertEqual(self.tier(make_usage(tier="Gold")), "Gold")
        self.assertEqual(self.tier(make_usage(), account={"plan": "Team"}), "Team")
        self.assertIsNone(self.tier(make_usage(), account=None))
        self.assertIsNone(self.tier(make_usage()))

    def test_unsafe_plan_text_is_dropped(self):
        self.assertIsNone(self.tier(make_usage(loginMethod="me@example.com")))
        self.assertIsNone(self.tier(make_usage(loginMethod="x" * 65)))
        self.assertIsNone(self.tier(make_usage(loginMethod=5)))


class NormalizeSafetyTests(unittest.TestCase):
    def test_provider_mismatch_is_rejected(self):
        cursor = make_entry("cursor", "web", make_usage(secondary=win(1, 10080), identity=ident("cursor")))
        codex = make_entry("codex", "oauth", make_usage(secondary=win(1, 10080), identity=ident("codex")))
        wrong_identity = make_entry("codex", "oauth", make_usage(secondary=win(1, 10080), identity=ident("zai")))
        missing_provider = {"source": "oauth", "usage": make_usage(secondary=win(1, 10080))}
        for name, payload in {
            "other-provider": [cursor],
            "mixed": [codex, cursor],
            "identity-provider": [wrong_identity],
            "missing-provider": [missing_provider],
        }.items():
            with self.subTest(name):
                hasher = RecordingHasher()
                result = norm(payload, "codex", hasher)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["error"]["code"], "provider_mismatch")
                self.assertEqual(result["windows"], [])
                self.assertIsNone(result["account_ref"])
                self.assertEqual(hasher.calls, [])

    def test_multiple_accounts_are_ambiguous(self):
        first = make_entry("codex", "oauth", make_usage(secondary=win(1, 10080), identity=ident("codex")))
        second = make_entry(
            "codex", "oauth",
            make_usage(secondary=win(2, 10080), identity=ident("codex", accountEmail="other@example.com")),
        )
        hasher = RecordingHasher()
        result = norm([first, second], "codex", hasher)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "ambiguous_accounts")
        self.assertEqual(result["windows"], [])
        self.assertEqual(hasher.calls, [])

    def test_invalid_payloads(self):
        for payload in (None, 5, "text", [], [1], [{}], {}, [{"foo": "bar"}]):
            with self.subTest(payload=repr(payload)):
                result = norm(payload, "codex")
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["error"]["code"], "invalid_payload")
                self.assertEqual(result["windows"], [])

    def test_non_dict_usage_and_null_usage(self):
        result = norm([make_entry("codex", "oauth", "junk")], "codex")
        self.assertEqual(result["error"]["code"], "invalid_payload")
        result = norm([make_entry("codex", "oauth", None)], "codex")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["error"]["code"], "no_quota_data")

    def test_unsupported_provider_is_rejected(self):
        with self.assertRaises(ValueError):
            codexbar.normalize([], "muse")
        with self.assertRaises(ValueError):
            codexbar.normalize([], None)

    def test_pii_and_secrets_never_reach_result(self):
        secrets = ["secret.person@example.com", "acct-SECRET-0000-1111", "tok-SECRET", "session=SECRET"]
        identity = {
            "providerID": "codex",
            "accountEmail": secrets[0],
            "accountID": secrets[1],
            "loginMethod": "pro",
            "accessToken": secrets[2],
        }
        usage = make_usage(secondary=win(10, 10080), identity=identity, cookie=secrets[3])
        entry = make_entry("codex", "oauth", usage, accessToken=secrets[2], raw={"email": secrets[0]})
        hasher = RecordingHasher("ref-abc")
        result = norm([entry], "codex", hasher)

        dump = json.dumps(result)
        for secret in secrets:
            self.assertNotIn(secret, dump)
        self.assertEqual(result["account_ref"], "ref-abc")
        self.assertEqual(set(result), CANONICAL_KEYS)
        for window in result["windows"]:
            self.assertEqual(set(window), WINDOW_KEYS)
        self.assertEqual(
            hasher.calls,
            [("codex", {"accountEmail": secrets[0], "accountID": secrets[1], "loginMethod": "pro"})],
        )

    def test_hasher_misbehaviour_yields_no_account_ref(self):
        usage = make_usage(secondary=win(10, 10080), identity=ident("codex"))
        payload = [make_entry("codex", "oauth", usage)]

        def raising(provider_id, identity):
            raise ValueError(identity["accountEmail"])

        for name, hasher in {
            "returns-email": lambda p, i: "x-" + i["accountEmail"].upper(),
            "returns-account-id": lambda p, i: "x-" + i["accountID"],
            "raises": raising,
            "non-string": lambda p, i: 123,
            "empty": lambda p, i: "  ",
            "multiline": lambda p, i: "a\nb",
            "not-callable": "hasher",
        }.items():
            with self.subTest(name):
                result = norm(payload, "codex", hasher)
                self.assertIsNone(result["account_ref"])
                self.assertEqual(result["status"], "verified")
                self.assertNotIn(EMAIL, json.dumps(result))

    def test_hasher_not_called_for_plan_only_identity(self):
        hasher = RecordingHasher()
        usage = make_usage(secondary=win(10, 10080), identity={"providerID": "codex", "loginMethod": "pro"})
        result = norm([make_entry("codex", "oauth", usage)], "codex", hasher)
        self.assertEqual(hasher.calls, [])
        self.assertIsNone(result["account_ref"])
        self.assertEqual(result["status"], "verified")

    def test_error_entries_are_classified_with_constant_messages(self):
        cases = [
            ({"code": 1, "message": "Not logged in. Run login for secret@example.com", "kind": "provider"},
             "auth_required", "auth_required"),
            ("HTTP 401 from provider", "auth_required", "auth_required"),
            ({"message": "Network connection lost for secret@example.com"}, "unavailable", "provider_unavailable"),
            ({"message": "Something exploded sk-live-123456"}, "error", "provider_error"),
            (True, "error", "provider_error"),
        ]
        for error, status, code in cases:
            with self.subTest(error=repr(error)):
                result = norm([{"provider": "codex", "source": "oauth", "error": error}], "codex")
                self.assertEqual(result["status"], status)
                self.assertEqual(result["error"]["code"], code)
                self.assertEqual(result["error"]["message"], codexbar._ERRORS[code][1])
                self.assertEqual(result["windows"], [])
                self.assertIs(result["auth_verified"], False)
                dump = json.dumps(result)
                self.assertNotIn("secret@example.com", dump)
                self.assertNotIn("sk-live", dump)

    def test_error_alongside_valid_data_keeps_data_as_partial(self):
        usage = make_usage(secondary=win(10, 10080), identity=ident("codex"))
        entry = make_entry("codex", "oauth", usage, error={"message": "boom"})
        result = norm([entry], "codex")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["windows"]), 1)
        self.assertEqual(result["error"]["code"], "provider_error")

    def test_source_names_are_sanitized(self):
        usage = make_usage(secondary=win(10, 10080), identity=ident("codex"))
        for raw, expected, method in (
            ("OAuth", "codexbar:oauth", "oauth"),
            ("openai-web", "codexbar:openai-web", "session"),
            ("cli", "codexbar:cli", "unknown"),
            ("bad source!", "codexbar:unknown", "unknown"),
            (None, "codexbar:unknown", "unknown"),
        ):
            with self.subTest(raw=raw):
                result = norm([make_entry("codex", raw, usage)], "codex")
                self.assertEqual(result["source"], expected)
                self.assertEqual(result["auth_method"], method)


def run_result(stdout=b"", stderr=b"", returncode=0, outcome="ok"):
    return codexbar._RunResult(outcome, returncode, stdout, stderr)


def codex_payload_bytes(source="oauth"):
    usage = make_usage(secondary=win(43, 10080, "2026-10-18T04:01:44Z"), identity=ident("codex", loginMethod="promax"))
    return json.dumps([make_entry("codex", source, usage)]).encode()


class CollectTests(unittest.TestCase):
    def collect_with(self, provider, result, **kwargs):
        with mock.patch.object(codexbar, "_run_process", return_value=result) as run:
            collected = codexbar.collect(provider, **kwargs)
        return collected, run

    def test_command_line_per_provider(self):
        self.assertEqual(codexbar.CODEXBAR_PATH, "/opt/homebrew/bin/codexbar")
        self.assertTrue(os.path.isabs(codexbar.CODEXBAR_PATH))
        for provider, source in (("codex", "oauth"), ("cursor", "auto"), ("antigravity", "auto"), ("zai", "auto")):
            with self.subTest(provider):
                _, run = self.collect_with(provider, run_result(b"[]"), timeout_seconds=12)
                argv, timeout = run.call_args[0]
                self.assertEqual(
                    argv,
                    [
                        "/opt/homebrew/bin/codexbar", "usage", "--provider", provider, "--source", source,
                        "--format", "json", "--json-only", "--no-color", "--log-level", "error",
                    ],
                )
                self.assertEqual(timeout, 12)

    def test_default_timeout_is_seventy_five_seconds(self):
        _, run = self.collect_with("codex", run_result(b"[]"))
        self.assertEqual(run.call_args[0][1], 75)

    def test_valid_payload_is_preserved_on_nonzero_exit(self):
        result, _ = self.collect_with("codex", run_result(codex_payload_bytes(), b"warning", returncode=1))
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["windows"][0]["remaining_pct"], 57.0)
        self.assertEqual(result["source"], "codexbar:oauth")

    def test_actual_source_comes_from_payload(self):
        usage = make_usage(primary=win(0, 300), identity=ident("zai", loginMethod="max"))
        stdout = json.dumps([make_entry("zai", "api", usage)]).encode()
        result, _ = self.collect_with("zai", run_result(stdout))
        self.assertEqual(result["source"], "codexbar:api")
        self.assertEqual(result["auth_method"], "api_key")

    def test_hasher_is_forwarded(self):
        hasher = RecordingHasher("ref-9")
        result, _ = self.collect_with("codex", run_result(codex_payload_bytes()), identity_hasher=hasher)
        self.assertEqual(result["account_ref"], "ref-9")
        self.assertEqual(hasher.calls[0][0], "codex")

    def test_process_failures_use_constant_messages(self):
        cases = [
            ("spawn_failed", "unavailable", "codexbar_unavailable"),
            ("timeout", "error", "timeout"),
            ("overflow", "error", "output_too_large"),
        ]
        for outcome, status, code in cases:
            with self.subTest(outcome):
                result, _ = self.collect_with(
                    "codex", run_result(codex_payload_bytes(), b"secret@example.com", None, outcome)
                )
                self.assertEqual(result["status"], status)
                self.assertEqual(result["error"], {"code": code, "message": codexbar._ERRORS[code][1]})
                self.assertEqual(result["source"], "codexbar:oauth")
                self.assertEqual(result["windows"], [])
                self.assertNotIn("secret@example.com", json.dumps(result))

    def test_unparseable_output_is_classified_without_echo(self):
        cases = [
            (b"oops", b"Error: not logged in as secret@example.com", 1, "auth_required"),
            (b"", b"connection refused for secret@example.com", 1, "provider_unavailable"),
            (b"", b"kaboom secret@example.com", 1, "codexbar_failed"),
            (b"", b"", 0, "invalid_payload"),
        ]
        for stdout, stderr, returncode, code in cases:
            with self.subTest(code=code):
                result, _ = self.collect_with("cursor", run_result(stdout, stderr, returncode))
                self.assertEqual(result["error"]["code"], code)
                self.assertEqual(result["error"]["message"], codexbar._ERRORS[code][1])
                self.assertEqual(result["source"], "codexbar:auto")
                self.assertNotIn("secret@example.com", json.dumps(result))

    def test_parsed_json_is_not_classified_from_raw_text(self):
        result, _ = self.collect_with("codex", run_result(b'[{"loginMethod": "pro"}]'))
        self.assertEqual(result["error"]["code"], "invalid_payload")

    def test_provider_mismatch_is_reported_with_requested_source(self):
        usage = make_usage(secondary=win(1, 10080), identity=ident("cursor"))
        stdout = json.dumps([make_entry("cursor", "web", usage)]).encode()
        result, _ = self.collect_with("codex", run_result(stdout))
        self.assertEqual(result["error"]["code"], "provider_mismatch")
        self.assertEqual(result["source"], "codexbar:oauth")

    def test_bad_arguments_are_rejected_before_running(self):
        with mock.patch.object(codexbar, "_run_process") as run:
            for provider in ("muse", "", None, "codex --source web"):
                with self.assertRaises(ValueError):
                    codexbar.collect(provider)
            for timeout in (0, -1, True, float("nan"), float("inf"), 601, "75"):
                with self.assertRaises(ValueError):
                    codexbar.collect("codex", timeout_seconds=timeout)
        run.assert_not_called()


def python_argv(code):
    return [sys.executable, "-c", code]


class RunProcessTests(unittest.TestCase):
    def test_captures_output_and_exit_code(self):
        code = "import sys; sys.stdout.write('{}'); sys.stderr.write('e'); sys.exit(3)"
        result = codexbar._run_process(python_argv(code), 20)
        self.assertEqual((result.outcome, result.returncode, result.stdout, result.stderr), ("ok", 3, b"{}", b"e"))

    def test_timeout_kills_process(self):
        started = time.monotonic()
        result = codexbar._run_process(python_argv("import time; time.sleep(60)"), 0.3)
        self.assertEqual(result.outcome, "timeout")
        self.assertEqual((result.stdout, result.stderr), (b"", b""))
        self.assertLess(time.monotonic() - started, 20)

    def test_output_over_limit_is_rejected_and_process_killed(self):
        code = "import sys, time; sys.stdout.buffer.write(b'x' * 200000); sys.stdout.flush(); time.sleep(60)"
        started = time.monotonic()
        result = codexbar._run_process(python_argv(code), 30, max_output_bytes=1000)
        self.assertEqual(result.outcome, "overflow")
        self.assertEqual(result.stdout, b"")
        self.assertLess(time.monotonic() - started, 20)

    def test_output_limit_boundary(self):
        template = "import sys; sys.stdout.buffer.write(b'x' * {}); sys.stdout.flush()"
        exact = codexbar._run_process(python_argv(template.format(1000)), 20, max_output_bytes=1000)
        self.assertEqual((exact.outcome, len(exact.stdout)), ("ok", 1000))
        over = codexbar._run_process(python_argv(template.format(1001)), 20, max_output_bytes=1000)
        self.assertEqual(over.outcome, "overflow")

    def test_missing_binary_is_spawn_failure(self):
        result = codexbar._run_process(["/nonexistent/aiusage-test-binary"], 5)
        self.assertEqual(result.outcome, "spawn_failed")
        self.assertIsNone(result.returncode)

    def test_output_limit_default_is_two_mebibytes(self):
        self.assertEqual(codexbar.MAX_OUTPUT_BYTES, 2 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
