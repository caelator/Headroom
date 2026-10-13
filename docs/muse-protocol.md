# Integrated Muse Code quota collector

The separate browser-extension prototype was replaced by a direct collector in `src/aiusage/muse.py`. It is historical work only and is excluded from the source repository. There are no public browser bridge/challenge/capture routes in the running service.

## Reverse-engineering evidence

The installed Muse Code 1.4.2-R4684.1 executable advertises subscription quota frames, the `usage/read` schema and its account-token credential endpoint. Its offline exported schema defines current window usage, rolling weekly usage, tier and point-in-time observation timestamps. The executable contains the product path `muse-code/key`, API origin `https://api.meta.ai`, and header version `x-api-version: 1.0.0`.

A live authenticated POST to that fixed endpoint established the actual wire response: `is_subs_active`, `subs_tier_name`, `subs_tier_id`, and `subs_usage` with `window` / `weekly`. Both blocks contain `used_percent` and `resets_at` in epoch seconds; the current window also supplies `window_duration_mins`. The collector uses those exact fields. It does not infer allowance from Meta's separate API billing wallet or from desktop Muse's `/hatch/subscription` interface.

The collector verifies the active subscription and reported tier from the authenticated response. Public examples use synthetic values; account-specific subscription details and captured quota readings are omitted. The dashboard refreshes live readings locally.

## Boundaries

The credential belongs to the existing Muse CLI OAuth login. Reads are limited to its exact Keychain service/account and bounded to five seconds. Requests use TLS validation, refuse redirects and ignore environment proxies. The endpoint response is bounded to 1 MiB. The client discards returned API keys, names, emails, avatars and payment fields after producing only tier, quota, reset, source and an HMAC identity. The keychain remains CLI-owned and is never rewritten by collection.

The live endpoint returned the same existing plan key on repeat polling, and the saved plan key continued to authenticate the model catalog. No model completion or chargeable prompt was used to obtain usage. If the endpoint's credential behavior changes, suspend collection and review that contract before altering credentials. Missing/malformed quota remains unknown; failures immediately invalidate current readings and use explicit last-known history.
