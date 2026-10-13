"""Reset/account-aware quota rates; tokens never imply plan quota percentages."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from statistics import median
import math


def utc_now():
    return datetime.now(timezone.utc)


def iso(value=None):
    return (value or utc_now()).astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def timestamp(value):
    try:
        if not isinstance(value, str): return None
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (ValueError, OverflowError):
        return None


def number(value):
    if isinstance(value, bool) or value is None: return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and abs(result) <= Decimal('1e15') else None
    except (InvalidOperation, ValueError):
        return None


def percentage(value):
    result = number(value)
    return result if result is not None and result >= 0 else None


def velocity(points, current, now=None, max_gap_seconds=1800):
    now = now or utc_now()
    result = {'pct_per_hour': None, 'points': 0, 'observed_span_seconds': 0,
              'confidence': 'insufficient', 'eta_exhaustion_at': None,
              'exhausts_before_reset': None, 'predicted_remaining_at_reset': None,
              'safe_pace_pct_per_hour': None, 'is_exhausted': False}
    remain = number(current.get('remaining_pct'))
    reset = timestamp(current.get('resets_at'))
    if current.get('status') in ('stale', 'unknown', 'expired', 'unlimited') or remain is None:
        result['confidence'] = 'unavailable'
        return result
    if reset and reset <= now:
        result['confidence'] = 'unavailable'
        return result
    result['is_exhausted'] = remain == 0
    if reset and reset > now:
        hours = Decimal(str((reset-now).total_seconds())) / 3600
        result['safe_pace_pct_per_hour'] = float(remain / hours)
    clean = []
    current_account = current.get('account_ref')
    if not current_account:
        result['confidence'] = 'unavailable'
        return result
    for p in points:
        stamp = timestamp(p.get('observed_at'))
        used = percentage(p.get('used_pct'))
        if not stamp or used is None or stamp > now + timedelta(seconds=30): continue
        if p.get('account_ref') != current_account: continue
        previous_reset = timestamp(p.get('resets_at'))
        # APIs may add sub-second/one-second jitter to the same reset deadline.
        # Actual new cycles are hours apart; never merge them or missing deadlines.
        if previous_reset or reset:
            if not previous_reset or not reset or abs((previous_reset-reset).total_seconds()) > 2: continue
        elif p.get('resets_at') != current.get('resets_at'): continue
        if p.get('window_minutes') != current.get('window_minutes'): continue
        if p.get('status') != 'fresh': continue
        clean.append((stamp, used))
    clean.sort(key=lambda p: p[0])
    # Keep a contiguous monotone segment from the most recent current cycle.
    segment = []
    for stamp, used in clean:
        if segment:
            previous_time, previous_used = segment[-1]
            seconds = (stamp - previous_time).total_seconds()
            if seconds <= 0:
                if used != previous_used: segment = []
                else: continue
            elif seconds > max_gap_seconds or used < previous_used:
                segment = []  # Recovery/reset/correction is not negative consumption.
        segment.append((stamp, used))
    if not segment: return result
    if (now-segment[-1][0]).total_seconds() > max_gap_seconds:
        result['confidence'] = 'unavailable'
        return result
    span = (segment[-1][0]-segment[0][0]).total_seconds()
    result.update(points=len(segment), observed_span_seconds=round(span, 3))
    if len(segment) < 3 or span < 300: return result
    # Long-baseline pairwise slopes resist minute-level rounded/unchanged samples.
    slopes = []
    for i, (start, used_start) in enumerate(segment):
        for end, used_end in segment[i+1:]:
            elapsed = Decimal(str((end-start).total_seconds()))
            if elapsed >= 300:
                slopes.append((used_end-used_start) * 3600 / elapsed)
    if not slopes: return result
    rate = median(slopes)
    if rate < 0: return result
    result['confidence'] = 'observed'
    result['pct_per_hour'] = float(rate)
    if rate == 0 or remain == 0:
        # Rounded quotas can hide small consumption; a flat trace is not a forecast.
        return result
    hours_left = remain / rate
    # Bound timestamp math rather than inventing an implausible exhaustion date.
    if hours_left <= 24 * 365:
        eta = now + timedelta(seconds=float(hours_left * 3600))
        result['eta_exhaustion_at'] = iso(eta)
        if reset:
            result['exhausts_before_reset'] = eta < reset
            remaining_at_reset = remain - rate * Decimal(str((reset-now).total_seconds())) / 3600
            result['predicted_remaining_at_reset'] = float(max(Decimal(0), remaining_at_reset))
    return result
