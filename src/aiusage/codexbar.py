"""Normalize CodexBar CLI quota JSON into safe canonical snapshots."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import signal
import subprocess
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import NamedTuple, Optional

__all__ = ["collect", "normalize"]

CODEXBAR_PATH = "/opt/homebrew/bin/codexbar"
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
SUPPORTED_PROVIDERS = ("codex", "cursor", "antigravity", "zai", "claude")

_MAX_STDERR_BYTES = 256 * 1024
_MAX_CLASSIFY_CHARS = 64 * 1024
_MAX_TIMEOUT_SECONDS = 600
_SOURCE_ARGUMENT = {"codex": "oauth", "cursor": "auto", "antigravity": "auto", "zai": "auto", "claude": "oauth"}

_SLOTS = ("primary", "secondary", "tertiary")
_SLOT_TITLES = {"primary": "Primary limit", "secondary": "Secondary limit", "tertiary": "Tertiary limit"}
_CURSOR_SLOT_LABELS = {"primary": "Included plan", "secondary": "Cursor models", "tertiary": "Third-party models"}
_MAX_EXTRA_WINDOWS = 32
_MAX_WINDOW_MINUTES = 10 * 366 * 24 * 60
_FUTURE_SKEW = timedelta(minutes=5)
_RECOGNIZED_KEYS = frozenset({"provider", "source", "usage", "error"})

_CONFIDENT_VALUES = frozenset({"high", "verified", "measured", "exact", "provider", "full", "complete"})
_UNAVAILABLE_KEYS = ("unavailable", "isUnavailable", "limitsUnavailable")

_SOURCE_RE = re.compile(r"[a-z0-9][a-z0-9._+-]{0,39}")
_UNIT_RE = re.compile(r"[A-Za-z][A-Za-z0-9 _-]{0,23}")
_TIME_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})[T ]([0-9]{2}):([0-9]{2})"
    r"(?::([0-9]{2})(?:[.,]([0-9]+))?)?(Z|z|[+-][0-9]{2}(?::?[0-9]{2})?)"
)

_AUTH_HINTS = (
    "not logged in", "not signed in", "signed out", "login required", "log in", "login",
    "sign in", "sign-in", "signin", "unauthorized", "unauthenticated", "authentication",
    "auth required", "invalid token", "token expired", "expired token", "token has expired",
    "session expired", "no session", "missing session", "credential", "cookie", "api key",
    "apikey",
)
_UNAVAILABLE_HINTS = (
    "not installed", "not found", "not enabled", "disabled", "unsupported", "not supported",
    "unavailable", "not available", "no data", "offline", "network", "connection",
    "could not connect", "unreachable", "econnrefused", "unknown provider", "no such provider",
    "dns",
)

# code -> (status, constant message); messages never carry upstream text.
_ERRORS = {
    "auth_required": ("auth_required", "Provider authentication is required."),
    "provider_unavailable": ("unavailable", "Provider usage is unavailable."),
    "codexbar_unavailable": ("unavailable", "CodexBar is not available."),
    "no_quota_data": ("unavailable", "No quota data was reported."),
    "provider_error": ("error", "Provider usage could not be read."),
    "codexbar_failed": ("error", "CodexBar failed to report usage."),
    "timeout": ("error", "CodexBar timed out."),
    "output_too_large": ("error", "CodexBar output exceeded the size limit."),
    "invalid_payload": ("error", "CodexBar returned an unrecognized payload."),
    "provider_mismatch": ("error", "CodexBar returned data for a different provider."),
    "ambiguous_accounts": ("error", "CodexBar returned multiple accounts."),
}
_OUTCOME_CODES = {"spawn_failed": "codexbar_unavailable", "timeout": "timeout", "overflow": "output_too_large"}

_INVALID = object()


def collect(provider_id, identity_hasher=None, timeout_seconds=75):
    provider_id = _require_provider(provider_id)
    timeout = _require_timeout(timeout_seconds)
    requested = _SOURCE_ARGUMENT[provider_id]
    requested_source = "codexbar:" + requested
    argv = [
        CODEXBAR_PATH, "usage", "--provider", provider_id, "--source", requested,
        "--format", "json", "--json-only", "--no-color", "--log-level", "error",
    ]
    run = _run_process(argv, timeout)
    now = datetime.now(timezone.utc)

    if run.outcome in _OUTCOME_CODES:
        return _failure(provider_id, _OUTCOME_CODES[run.outcome], now, requested_source)

    payload = _parse_json(run.stdout)
    if payload is not _INVALID:
        result = normalize(payload, provider_id, identity_hasher, now=now)
        code = result["error"]["code"] if result["error"] else None
        if result["status"] in ("verified", "partial"):
            return result
        if code != "invalid_payload":
            if result["source"] == "codexbar:unknown":
                result["source"] = requested_source
            return result

    chunks = (run.stderr,) if payload is not _INVALID else (run.stdout, run.stderr)
    code = _classify(_classification_text(*chunks))
    if code is None:
        code = "invalid_payload" if run.returncode == 0 else "codexbar_failed"
    return _failure(provider_id, code, now, requested_source)


def normalize(payload, provider_id, identity_hasher=None, *, now=None):
    provider_id = _require_provider(provider_id)
    now = now.astimezone(timezone.utc) if now is not None else datetime.now(timezone.utc)
    try:
        return _normalize(payload, provider_id, identity_hasher, now)
    except Exception:
        return _failure(provider_id, "invalid_payload", now)


def _normalize(payload, provider_id, hasher, now):
    entries = _entries(payload)
    if entries is None or any(_RECOGNIZED_KEYS.isdisjoint(entry) for entry in entries):
        return _failure(provider_id, "invalid_payload", now)
    if not all(_entry_matches(entry, provider_id) for entry in entries):
        return _failure(provider_id, "provider_mismatch", now)
    if len(entries) > 1:
        return _failure(provider_id, "ambiguous_accounts", now)
    return _from_entry(entries[0], provider_id, hasher, now)


def _entries(payload):
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list) and payload and all(isinstance(item, dict) for item in payload):
        return payload
    return None


def _entry_matches(entry, provider_id):
    usage = entry.get("usage")
    declared = _provider_key(entry.get("provider"))
    if declared is None:
        if isinstance(usage, dict):
            return False
    elif declared != provider_id:
        return False
    identity = usage.get("identity") if isinstance(usage, dict) else None
    if isinstance(identity, dict):
        nested = _provider_key(identity.get("providerID"))
        if nested is not None and nested != provider_id:
            return False
    return True


def _from_entry(entry, provider_id, hasher, now):
    source_name = _source_name(entry.get("source"))
    source = "codexbar:" + source_name
    auth_method = _auth_method(source_name)
    error = entry.get("error")
    error_code = (_classify(_error_text(error)) or "provider_error") if error else None

    usage = entry.get("usage")
    if usage is not None and not isinstance(usage, dict):
        return _failure(provider_id, error_code or "invalid_payload", now, source, auth_method)
    if usage is None:
        return _failure(provider_id, error_code or "no_quota_data", now, source, auth_method)

    observed = _parse_time(usage.get("updatedAt"))
    if observed is None or observed > now + _FUTURE_SKEW:
        observed = now
    observed_at = _iso(observed)

    raw_identity = usage.get("identity")
    raw_identity = raw_identity if isinstance(raw_identity, dict) else {}
    identity = {
        "accountEmail": _identity_text(raw_identity.get("accountEmail"), 320),
        "accountID": _identity_text(raw_identity.get("accountID"), 256),
        "loginMethod": _identity_text(raw_identity.get("loginMethod"), 128)
        or _identity_text(usage.get("loginMethod"), 128),
    }

    windows, incomplete = _collect_windows(usage, provider_id, source, observed_at)
    degraded = _degraded(usage) or _degraded(entry)
    if degraded or _looks_like_availability(provider_id, windows):
        for window in windows:
            window.update(used_pct=None, remaining_pct=None, status="unknown")

    wallet = _wallet(usage.get("providerCost"))
    known = any(window["status"] in ("fresh", "unlimited") for window in windows)
    if not (known or wallet is not None or any(identity.values())):
        return _failure(provider_id, error_code or "no_quota_data", now, source, auth_method)

    complete = (
        known
        and not incomplete
        and not degraded
        and error_code is None
        and all(window["status"] in ("fresh", "unlimited") for window in windows)
    )
    return _snapshot(
        provider_id,
        observed_at,
        source,
        "verified" if complete else "partial",
        _tier(entry, usage, raw_identity),
        auth_method,
        True,
        _account_ref(provider_id, identity, hasher),
        windows,
        wallet,
        {"code": error_code, "message": _ERRORS[error_code][1]} if error_code else None,
    )


def _snapshot(provider_id, observed_at, source, status, tier, auth_method, auth_verified,
              account_ref, windows, wallet, error):
    return {
        "id": provider_id,
        "observed_at": observed_at,
        "source": source,
        "status": status,
        "verified_tier": tier,
        "auth_method": auth_method,
        "auth_verified": auth_verified,
        "account_ref": account_ref,
        "windows": windows,
        "wallet": wallet,
        "error": error,
    }


def _failure(provider_id, code, now, source="codexbar:unknown", auth_method="unknown"):
    status, message = _ERRORS[code]
    return _snapshot(
        provider_id, _iso(now), source, status, None, auth_method, False, None, [], None,
        {"code": code, "message": message},
    )


def _require_provider(provider_id):
    if not isinstance(provider_id, str) or provider_id not in SUPPORTED_PROVIDERS:
        raise ValueError("unsupported provider")
    return provider_id


def _require_timeout(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 < value <= _MAX_TIMEOUT_SECONDS
    ):
        raise ValueError("invalid timeout")
    return float(value)


def _provider_key(value):
    return value.strip().lower() if isinstance(value, str) else None


def _source_name(value):
    if isinstance(value, str):
        name = value.strip().lower()
        if _SOURCE_RE.fullmatch(name):
            return name
    return "unknown"


def _auth_method(source_name):
    tokens = set(re.split(r"[^a-z0-9]+", source_name))
    if "oauth" in tokens:
        return "oauth"
    if tokens & {"web", "session", "cookie", "cookies", "browser", "app"}:
        return "session"
    if tokens & {"api", "apikey", "key", "token"}:
        return "api_key"
    return "unknown"


def _tier(entry, usage, raw_identity):
    candidates = [raw_identity.get("loginMethod"), usage.get("loginMethod")]
    for holder in (usage, entry.get("account")):
        if isinstance(holder, dict):
            candidates.extend(holder.get(key) for key in ("plan", "planName", "tier"))
    for candidate in candidates:
        text = _text(candidate, 64)
        if text and "@" not in text:
            return text
    return None


def _account_ref(provider_id, identity, hasher):
    if not callable(hasher):
        return None
    email, account_id = identity["accountEmail"], identity["accountID"]
    if email is None and account_id is None:
        return None
    try:
        ref = hasher(provider_id, {key: value for key, value in identity.items() if value})
    except Exception:
        return None
    if not isinstance(ref, str):
        return None
    ref = ref.strip()
    if not ref or len(ref) > 256 or not ref.isprintable():
        return None
    lowered = ref.lower()
    if email and email.lower() in lowered:
        return None
    if account_id and len(account_id) >= 8 and account_id.lower() in lowered:
        return None
    return ref


def _collect_windows(usage, provider_id, source, observed_at):
    windows = []
    incomplete = False
    for slot in _SLOTS:
        raw = usage.get(slot)
        if raw is None:
            continue
        window = _build_window(raw, None, source, observed_at)
        minutes = window["window_minutes"]
        window["id"] = slot if minutes is None else f"{slot}-{minutes}m"
        window["label"] = _slot_label(provider_id, slot, minutes)
        windows.append(window)

    extras = usage.get("extraRateWindows")
    if extras is None:
        return windows, incomplete
    if not isinstance(extras, list):
        return windows, True
    if len(extras) > _MAX_EXTRA_WINDOWS:
        incomplete = True

    drafts = []
    for item in extras[:_MAX_EXTRA_WINDOWS]:
        if not isinstance(item, dict):
            incomplete = True
            continue
        window = _build_window(item.get("window"), item, source, observed_at)
        minutes = window["window_minutes"]
        kind = _text(item.get("kind"), 80)
        provider_label = _label(item.get("label"))
        title = _label(item.get("title"))
        label = (
            provider_label or title or _label(item.get("kind")) or _duration_label(minutes)
            or "Additional limit"
        )
        slug = _slug(kind) or _slug(label) or "window"
        base = f"extra-{slug}" + (f"-{minutes}m" if minutes else "")
        descriptor = "\x00".join((kind or "", provider_label or "", title or ""))
        window["label"] = label
        drafts.append((base, descriptor, window))

    counts = Counter(base for base, _, _ in drafts)
    taken = {window["id"] for window in windows}
    for base, descriptor, window in drafts:
        candidate = base
        if counts[base] > 1:
            candidate = f"{base}-{hashlib.sha256(descriptor.encode('utf-8')).hexdigest()[:6]}"
        unique, ordinal = candidate, 1
        while unique in taken:
            ordinal += 1
            unique = f"{candidate}-{ordinal}"
        taken.add(unique)
        window["id"] = unique
        windows.append(window)
    return windows, incomplete


def _build_window(raw, wrapper, source, observed_at):
    window = {
        "id": "",
        "label": "",
        "used_pct": None,
        "remaining_pct": None,
        "resets_at": None,
        "window_minutes": None,
        "status": "unknown",
        "observed_at": observed_at,
        "source": source,
    }
    if not isinstance(raw, dict):
        return window
    window["resets_at"] = _iso_or_none(raw.get("resetsAt"))
    window["window_minutes"] = _minutes(raw.get("windowMinutes"))
    holders = [raw] if wrapper is None else [raw, wrapper]
    if any(_degraded(holder) for holder in holders):
        return window
    if any(holder.get("unlimited") is True or holder.get("isUnlimited") is True for holder in holders):
        window["status"] = "unlimited"
        return window
    used = _number(raw.get("usedPercent"))
    if used is not None:
        window.update(used_pct=used, remaining_pct=max(0.0, 100.0 - used), status="fresh")
    return window


def _looks_like_availability(provider_id, windows):
    # Google model availability reports 100% everywhere with no duration or reset; that is not quota.
    return (
        provider_id == "antigravity"
        and bool(windows)
        and all(
            window["status"] == "fresh"
            and window["used_pct"] == 0.0
            and window["window_minutes"] is None
            and window["resets_at"] is None
            for window in windows
        )
    )


def _degraded(holder):
    if not isinstance(holder, dict):
        return False
    for key in _UNAVAILABLE_KEYS:
        value = holder.get(key)
        if value is True or (isinstance(value, str) and value.strip()):
            return True
    confidence = holder.get("dataConfidence")
    if confidence is None:
        return False
    return not (isinstance(confidence, str) and confidence.strip().lower() in _CONFIDENT_VALUES)


def _wallet(cost):
    if not isinstance(cost, dict):
        return None
    balance = _number(cost.get("balance"))
    unit = _text(cost.get("currencyCode"), 24)
    if balance is None or unit is None or not _UNIT_RE.fullmatch(unit):
        return None
    try:
        text = format(Decimal(repr(balance)), "f")
    except InvalidOperation:
        return None
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return {"balance": text, "unit": unit, "label": "Balance"}


def _slot_label(provider_id, slot, minutes):
    if provider_id == "cursor":
        return _CURSOR_SLOT_LABELS[slot]
    return _duration_label(minutes) or _SLOT_TITLES[slot]


def _duration_label(minutes):
    if minutes is None:
        return None
    if minutes == 10080:
        return "Weekly limit"
    if minutes == 43200:
        return "Monthly limit"
    if minutes == 1440:
        return "Daily limit"
    if minutes % 10080 == 0:
        return f"{minutes // 10080}-week limit"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}-day limit"
    if minutes % 60 == 0:
        return f"{minutes // 60}-hour limit"
    return f"{minutes}-minute limit"


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number + 0.0


def _minutes(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        return None
    minutes = int(value)
    return minutes if 0 < minutes <= _MAX_WINDOW_MINUTES else None


def _text(value, limit):
    if not isinstance(value, str):
        return None
    cleaned = " ".join("".join(ch if ch.isprintable() else " " for ch in value).split())
    if not cleaned or len(cleaned) > limit:
        return None
    return cleaned


def _identity_text(value, limit):
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    return _text(value, limit)


def _label(value):
    text = _text(value, 80)
    return text if text and "@" not in text else None


def _slug(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:40].strip("-")


def _parse_time(value):
    if not isinstance(value, str):
        return None
    match = _TIME_RE.fullmatch(value.strip())
    if not match:
        return None
    year, month, day, hour, minute, second, fraction, zone = match.groups()
    try:
        if zone in ("Z", "z"):
            offset = timedelta(0)
        else:
            digits = zone[1:].replace(":", "")
            offset_hours, offset_minutes = int(digits[:2]), int(digits[2:4] or 0)
            if offset_hours > 23 or offset_minutes > 59:
                return None
            offset = timedelta(hours=offset_hours, minutes=offset_minutes)
            if zone[0] == "-":
                offset = -offset
        microsecond = int(fraction[:6].ljust(6, "0")) if fraction else 0
        local = datetime(
            int(year), int(month), int(day), int(hour), int(minute), int(second or 0),
            microsecond, tzinfo=timezone(offset),
        )
        return local.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _iso(moment):
    moment = moment.astimezone(timezone.utc)
    text = (
        f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}"
        f"T{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}"
    )
    if moment.microsecond:
        text += f".{moment.microsecond:06d}"
    return text + "Z"


def _iso_or_none(value):
    moment = _parse_time(value)
    return _iso(moment) if moment is not None else None


def _error_text(error):
    parts = []
    if isinstance(error, str):
        parts.append(error)
    elif isinstance(error, dict):
        for key in ("message", "kind", "description"):
            if isinstance(error.get(key), str):
                parts.append(error[key])
    return " ".join(parts).lower()[:_MAX_CLASSIFY_CHARS]


def _classify(text):
    if any(hint in text for hint in _AUTH_HINTS) or re.search(r"\b401\b", text):
        return "auth_required"
    if any(hint in text for hint in _UNAVAILABLE_HINTS):
        return "provider_unavailable"
    return None


def _classification_text(*chunks):
    text = " ".join(chunk[:_MAX_CLASSIFY_CHARS].decode("utf-8", errors="replace") for chunk in chunks)
    return text.lower()[:_MAX_CLASSIFY_CHARS]


def _parse_json(data):
    try:
        text = data.decode("utf-8").lstrip("﻿").strip()
        return json.loads(text) if text else _INVALID
    except (ValueError, RecursionError):
        return _INVALID


class _RunResult(NamedTuple):
    outcome: str
    returncode: Optional[int]
    stdout: bytes
    stderr: bytes


class _Reader(threading.Thread):
    def __init__(self, stream, limit, overflow):
        super().__init__(daemon=True)
        self._stream = stream
        self._limit = limit
        self._overflow = overflow
        self.data = bytearray()

    def run(self):
        try:
            while True:
                chunk = self._stream.read(65536)
                if not chunk:
                    return
                room = self._limit - len(self.data)
                if len(chunk) > room:
                    self.data.extend(chunk[:room])
                    if self._overflow is not None:
                        self._overflow.set()
                        return
                    continue
                self.data.extend(chunk)
        except (OSError, ValueError):
            return


def _kill_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass


def _terminate(proc):
    _kill_group(proc.pid)
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _run_process(argv, timeout_seconds, max_output_bytes=MAX_OUTPUT_BYTES):
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            bufsize=0,
            start_new_session=True,
        )
    except (OSError, ValueError):
        return _RunResult("spawn_failed", None, b"", b"")

    overflow = threading.Event()
    out_reader = _Reader(proc.stdout, max_output_bytes, overflow)
    err_reader = _Reader(proc.stderr, _MAX_STDERR_BYTES, None)
    outcome = "ok"
    try:
        out_reader.start()
        err_reader.start()
        deadline = time.monotonic() + timeout_seconds
        while proc.poll() is None:
            if overflow.is_set():
                outcome = "overflow"
                break
            if time.monotonic() >= deadline:
                outcome = "timeout"
                break
            overflow.wait(0.05)
    except BaseException:
        _terminate(proc)
        raise

    if outcome != "ok":
        _terminate(proc)
    readers = (out_reader, err_reader)
    for reader in readers:
        reader.join(2)
    if any(reader.is_alive() for reader in readers):
        # A descendant still holds a pipe open; its group id stays valid while it lives.
        _kill_group(proc.pid)
        for reader in readers:
            reader.join(2)
    if overflow.is_set():
        outcome = "overflow"
    for stream, reader in ((proc.stdout, out_reader), (proc.stderr, err_reader)):
        if not reader.is_alive():
            try:
                stream.close()
            except OSError:
                pass
    if outcome != "ok":
        return _RunResult(outcome, proc.returncode, b"", b"")
    return _RunResult("ok", proc.returncode, bytes(out_reader.data), bytes(err_reader.data))
