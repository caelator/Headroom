<div align="center">

![Headroom — know your remaining AI capacity](docs/assets/headroom.svg)

**Your AI plans. One local dashboard.**

See how much capacity you have left, how quickly you are using it, and when each allowance resets.

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![macOS 13+](https://img.shields.io/badge/macOS-13%2B-222222?logo=apple)](#requirements)
[![MIT license](https://img.shields.io/badge/License-MIT-397A61)](LICENSE)

[Get started](#quick-start) · [Providers](#supported-providers) · [How measurement works](docs/metrics.md) · [Contribute](CONTRIBUTING.md)

</div>

Headroom brings **Codex / ChatGPT, Muse, z.ai, Cursor, Antigravity, and Claude** into one view. It reads existing authenticated provider connections, keeps quota history on your machine, and shows reset-aware estimates when the observations support them. A native macOS menu companion keeps the most constrained measured plan within reach.

![Headroom dashboard with synthetic example data](docs/assets/dashboard.jpg)

*The preview renders the real dashboard with synthetic data. Percentages, tiers, and forecasts are illustrative.*

## What you get

- **Six plans at a glance:** remaining allowance, provider-reported tier, connection state, and the reset for the limiting window.
- **Observed burn and safe pace:** percentage points consumed per hour, conditional runout estimates, and the pace that would preserve quota until reset.
- **Quota history:** explore each provider and allowance over time; export stored snapshots as CSV with account identities omitted.
- **Local model accounting:** per-model receipts, known token counts, requests, and rolling token velocity, kept separate from subscription quota.
- **Freshness you can see:** expired readings and connection failures withdraw current percentages and forecasts while keeping labelled history.
- **Native menu indicator:** a macOS companion showing the smallest current remaining percentage and opening the dashboard.
- **Local storage:** SQLite history, private file permissions, and an HTTP service bound to `127.0.0.1`.

## Quick start

### Requirements

- **macOS 13+** for the supported provider integrations and menu companion.
- **Python 3.11+**. The Python runtime uses the standard library; there are no third-party runtime dependencies.
- **Node.js** to run the local JavaScript checks.
- **Codex, Cursor, and Antigravity**, signed in through their official clients, for the providers you use. Headroom includes its provider collectors; no CodexBar installation is needed.
- **Muse Code** and **Claude Code**, signed in through their official clients, for their direct OAuth collectors.
- **Xcode command-line tools** if you want the native menu companion (`xcode-select --install`).

Connect the providers you use; unavailable providers stay visible with their connection status. You do not need all six subscriptions to open the dashboard.

```sh
git clone https://github.com/caelator/Headroom.git
cd Headroom
make check PYTHON=python3
make run PYTHON=python3
```

Open **[http://127.0.0.1:8793/](http://127.0.0.1:8793/)**. Use **Settings** to inspect connections and adjust polling and warning thresholds.

The Makefile defaults to Homebrew's Python 3.11 path. Set `PYTHON` as above to use another Python 3.11+ installation. Run from the source checkout; there are no published binary releases yet.

### Start at login

```sh
make install PYTHON=python3
python3 scripts/install_menubar.py
```

The first command registers the dashboard as a per-user LaunchAgent. The second builds, ad-hoc signs, checks, and installs `~/Applications/Headroom.app`. Installation uses the absolute checkout path; reinstall the LaunchAgent after moving the source.

The menu installer replaces the owned `com.xxx.codex-usage-menu` companion and backs up its previous plist when present. Read [operations and recovery](docs/operations.md) for service names, diagnostics, backups, and rollback.

## Supported providers

| Provider | Quota source | Connection setup |
| --- | --- | --- |
| **Codex / ChatGPT** | Built-in Codex OAuth usage collector | Sign in to Codex. |
| **Muse** | Direct Muse Code account quota endpoint | Run `muse login`, then **Refresh Muse connection** in Settings. |
| **z.ai** | Built-in coding-plan quota API | Configure the existing coding-plan API key in the environment or supported coding client. |
| **Cursor** | Built-in authenticated usage APIs | Sign in to the Cursor desktop app. |
| **Antigravity** | Built-in local app quota endpoint | Keep the Antigravity app signed in and running. |
| **Claude** | Direct Claude OAuth usage and profile endpoints | Sign in to Claude Code; existing credential renewal is picked up on subsequent polls. |

Credentials stay in the provider's existing app or Keychain. Headroom does not ask you to paste passwords, tokens, cookies, or API keys into the dashboard. Provider tier labels are distinct from user-declared plan claims; a verified tier does not establish that it is the provider's highest available plan.

Provider endpoints and schemas can change. Session failures, Keychain access, rate limits, upstream delays, and offline periods can interrupt collection. Missing measurements remain unknown. See the [Muse collector protocol](docs/muse-protocol.md) for that integration's endpoint and credential boundaries.

## What the numbers mean

| Reading | Evidence and interpretation |
| --- | --- |
| **Quota remaining** | A provider percentage, or a known allowance in matching units and window. Percentages across plans are never added or averaged. |
| **Burn rate** | Observed percentage points per hour within one account and reset cycle. At least three valid points spanning five minutes are required. |
| **Runout forecast** | A conditional estimate assuming the observed rate continues. Resets, account changes, and gaps exceeding 90 seconds break continuity. |
| **Safe pace** | Remaining percentage divided by time until the reported reset. It can appear before enough history exists for a burn estimate. |
| **Local tokens** | Counters in local receipts. They do not establish billed cost or a subscription percentage. Unknown usage stays uncounted and labelled. |

Provider collection defaults to **30 seconds**, configurable from **10–60 seconds**. A reading expires after **90 seconds** or at its reset, whichever comes first. A collection or authentication failure immediately invalidates current quota. The browser and menu enforce expiry between API replies.

Local receipts are scanned every five seconds. Browser snapshots and the menu poll every ten seconds. Daily local model totals use **America/Bogota**; the other model ranges are rolling periods. Full formulas and accounting rules are in [measurement contracts](docs/metrics.md).

## Privacy and architecture

```text
Existing provider sign-ins              Local Codex receipts
            │                                   │
   Independent collectors                 Local accounting
            │                                   │
   Sanitized quota snapshots              Existing model ledger
            └───────────────┬───────────────────┘
                     Loopback API
                     127.0.0.1:8793
                       │        │
                 Dashboard   macOS menu
```

Quota history and settings live in `~/.local/share/aiusage/`. Account continuity uses a private HMAC salt; the existing model ledger is reused at `~/.codex/model-usage/data/usage.sqlite3`. Back up the quota directory as a unit so history and identity continuity stay aligned.

Headroom does not retain conversation text, raw account profiles, credentials, or cookies. Collectors contact fixed provider endpoints and refuse credential-bearing redirects. The local server validates Host and Origin and requires a local action header for writes. Public repository assets use synthetic data.

## Documentation and development

| Resource | Use it for |
| --- | --- |
| [Measurement contracts](docs/metrics.md) | Units, freshness, reset continuity, forecasting, and receipt provenance. |
| [Operations and recovery](docs/operations.md) | Installation, safe diagnostics, data backup, and rollback. |
| [Muse collector protocol](docs/muse-protocol.md) | Direct OAuth integration and sanitized response handling. |
| [Built-in provider collectors](docs/providers.md) | Authentication sources, integration boundaries, and migration. |
| [Third-party notices](THIRD_PARTY_NOTICES.md) | CodexBar protocol attribution and upstream license. |
| [Contributing](CONTRIBUTING.md) | Source layout, local checks, and changes to provider adapters. |
| [Security policy](SECURITY.md) | Private vulnerability reports and information to omit. |
| [Bug reports](https://github.com/caelator/Headroom/issues/new?template=bug_report.yml) | Reproducible problems with sanitized diagnostics. |
| [Feature requests](https://github.com/caelator/Headroom/issues/new?template=feature_request.yml) | Proposed improvements and concrete use cases. |

Run `make check PYTHON=python3` before submitting changes. The gate covers Python unit tests, HTTP boundaries, analytics, storage, collectors, receipt accounting, Python compilation, and JavaScript checks. Tests use synthetic fixtures and temporary databases. Passing local checks establishes the tested implementation; live provider availability is a separate operational condition.

Development and verification use local gates. Hosted CI for pushes, pull requests, and schedules is disabled.

Headroom is released under the [MIT license](LICENSE).
