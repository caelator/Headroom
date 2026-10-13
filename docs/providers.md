# Built-in provider collectors

Headroom owns collection and normalization for all six providers. No collector launches CodexBar or relies on its executable path. Protocol reference and attribution are recorded in [third-party notices](../THIRD_PARTY_NOTICES.md).

| Provider | Built-in source | Authentication owner |
| --- | --- | --- |
| Codex / ChatGPT | Authenticated Codex usage endpoint | Existing Codex OAuth sign-in. |
| Cursor | Cursor's authenticated usage APIs | Existing signed-in desktop app. |
| z.ai | Official coding-plan quota API | Existing coding-plan API key. |
| Antigravity | App-owned loopback language-server quota endpoint | Existing signed-in Antigravity app. |
| Muse | Muse Code account quota endpoint | Muse Code's existing Keychain OAuth credential. |
| Claude | Claude OAuth usage/profile endpoints | Claude Code's existing Keychain OAuth credential. |

The remote collectors use fixed official endpoints with TLS validation. Shared transport disables environment proxies, cookie jars, and redirects, bounds response size and request time, and never logs raw replies. Authentication failures and rate limits produce constant, sanitized errors.

Antigravity is a separate local protocol. Discovery accepts only this user's official app language-server processes and their listening sockets. Requests use numeric IPv4 loopback and the app's in-memory CSRF token. The app uses a self-signed TLS certificate; the certificate exception is confined to that process-owned loopback request. Redirects and proxies are disabled. No application, language server, prompt, or provider CLI is launched by the collector.

Antigravity's structured quota summary preserves explicit session and weekly bucket identities. Where the app supplies only per-model quotas, those models have distinct window IDs and no invented window duration. Model availability without a genuine quota measurement remains unknown. Separate quota and identity replies are accepted together only when the account is unchanged across the read.

## Credentials and migration

The dashboard does not ask for credentials. Sign in through the provider's existing application or configure its official coding client. Headroom reads credential material in memory and does not refresh, overwrite, or copy it into its quota database.

Existing CodexBar configuration can be read as an optional compatibility source for previously configured z.ai keys or Cursor sessions. It is not required for new installations. Existing credentials are never migrated into a new plaintext file. Account identity is hashed before crossing the collector boundary.

For z.ai, use `Z_AI_API_KEY` (or `ZAI_API_KEY`) in the environment that launches Headroom. The collector also reads a z.ai-configured Claude Code `env.ANTHROPIC_AUTH_TOKEN` or `env.ANTHROPIC_API_KEY` from `~/.claude/settings.json` only when `ANTHROPIC_BASE_URL` points to the matching official z.ai host. Set `Z_AI_REGION=bigmodel-cn` for BigModel China; its existing `BIGMODEL_API_KEY`, `ZHIPU_API_KEY`, `ZHIPUAI_API_KEY`, `GLM_API_KEY`, or supported official key files can supply the credential. Region and key scope remain separate. Custom remote endpoint overrides and team quota scopes are currently rejected rather than silently returning a personal quota.

Cursor uses `cursorAuth/accessToken` from the desktop app's own `state.vscdb`, opened read-only. An expired app credential requires renewal in Cursor. A previously configured manual session or legacy session file can be read as optional compatibility; Headroom does not extract credentials from general browser stores or create new cookie caches. Case-sensitive account IDs are preserved in authenticated requests. Individual, Cursor-model, and third-party-model pool percentages stay separate; an absent total is never replaced by an average of those pools.

Codex reads OAuth `tokens` from the active `CODEX_HOME/auth.json` (default `~/.codex/auth.json`). An API-key-only login does not establish subscription quota. Requests bind to the credential account, and a conflicting response identity is rejected. Model-specific limits and explicitly reported individual allowances remain separate from API credit balances.

Stored quota history and the private HMAC salt are preserved. Equivalent account/window identities retain continuity where the protocol supplies the same binding. A changed account, credential scope, allowance identity, reset, or cadence starts a new forecast segment. Historical source labels remain historical evidence; they are not rewritten as new collector measurements.

## Local verification

```sh
make check PYTHON=python3
python3 scripts/diagnostics.py --probe
```

The first command runs synthetic tests and source checks. The second exercises the actual built-in collectors in parallel using isolated temporary storage, prints only allowlisted provider status fields, and removes that temporary store afterward. It does not install or restart the dashboard. A provider can be unavailable because it is signed out, its app is closed, a credential needs renewal, or its endpoint is rate limited.

See [measurement contracts](metrics.md) for freshness and forecast rules and [operations](operations.md) for the installed service.
