# Measurement contracts

A quota observation belongs to exactly one provider, opaque account scope, allowance/window ID and observation timestamp. This is its database uniqueness key. Different plans, model pools and reset windows never share a denominator. Observations are append-only and deduplicated; current provider connection state is separate.

## Remaining quota

When the provider supplies used percentage, remaining is `max(0, 100 − used)`. A reported zero is a valid measurement; missing, malformed, nonfinite and negative counters remain unknown. Overage can exceed 100% used and is shown as 0% remaining. Explicit unlimited quotas remain unlimited with no percentage. The integrated Muse collector uses provider-reported coding quota percentages; API dollar balances cannot become coding quota percentages.

Current measurements must be fresh and not past their reset. Age ≥90 seconds, expired reset, collection failure or auth failure removes current used/remaining values and calculations. Last-known values have a separate field. A provider's headline selects an exhausted allowance first, then the earliest projected runout, otherwise the smallest current remaining percentage. The primary reset on a card belongs to that limiting allowance.

## Velocity and forecasting

Quota burn is measured in **percentage points per hour**. The calculation uses up to 120 recent points from six hours of history, within the same account, window duration and reset cycle. Reset deadline jitter up to two seconds is allowed because authenticated endpoints sometimes derive the same deadline with sub-second variation. Actual new cycles, quota decreases/recovery, gaps exceeding 90 seconds and changed identities break continuity.

At least three valid points spanning five minutes are required. The estimate is the median of pairwise slopes with at least five-minute separation. This reduces sensitivity to short-term rounding. If no change is measured, rate can be zero but exhaustion and reset projections remain withheld. Small usage may be hidden by the provider's reporting resolution.

For a positive observed rate, estimated time to exhaust is `remaining / rate`. Runout is marked only if its estimated timestamp precedes the reported reset. Estimated remaining at reset is clamped at zero. Safe pace is `remaining / hours until reset`; it is available with a current measured denominator and future reset, even before enough samples exist to estimate burn. A 0% allowance is already exhausted. All projections assume the observed rate continues; forecasts over one year are withheld.

## Model receipts

The local model ledger tracks arbitrary model IDs and merges them with the current installed model catalogs. Unused catalog models remain visible. Totals sum only reported counters; unknown usage and partial detail coverage are shown explicitly. Cached input is a subset of input, and reasoning output is a subset of output. Do not add those subsets again.

Receipts with shared response IDs are deduplicated. Legacy counter increments and model attribution inferred from saved turn context have separate provenance; unresolved router observations may overlap receipts and add no unknown tokens to known sums. Provider calls that were never recorded on this machine cannot be reconstructed. These counts do not establish billed dollars or subscription quota percentages.

Token velocity sums known local receipt tokens over rolling 15-, 60- and 360-minute windows and divides by elapsed minutes; request velocity is requests per hour. These include cached tokens. Quota velocity and token velocity are independent measurements. Today and daily groupings use America/Bogota; other model ranges are rolling days. Current token velocity is withheld when the collector's last successful scan exceeds 90 seconds or the client loses the API.

## Tier verification

Provider-reported tiers and user declarations are separate. HMAC account/org/team references permit continuity without retaining profile identifiers. Historical Codex quota imports require creator account agreement with the current authenticated Codex account and a recognized global Codex limit. Meta Accounts Center tier proof establishes the subscription name at verification time; it cannot establish a remaining percentage.

The integrated Muse Code collector reads the existing OAuth token from the CLI-owned Keychain item and calls its fixed account endpoint. The verified schema supplies `subs_usage.window.used_percent`, `window_duration_mins`, `resets_at` in epoch seconds, and the weekly block. The subscription must be active, and the authenticated email is HMACed for continuity. A request-send timestamp provides a conservative observation age. Returned API keys, profile fields and payment metadata are discarded; no coding inference is sent to obtain quota.
