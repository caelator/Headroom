# Operations and recovery

The dashboard service is `com.xxx.aiusage`; the owned menu companion remains `com.xxx.codex-usage-menu`. Both are per-user LaunchAgents. Their source lives in this checkout, their private logs live in `~/.local/share/aiusage/`, and their installed plists live in `~/Library/LaunchAgents/`.

```sh
curl --fail http://127.0.0.1:8793/health
python3 scripts/diagnostics.py
python3 scripts/diagnostics.py --probe
launchctl kickstart -k gui/$(id -u)/com.xxx.aiusage
```

The health response is a portfolio of six providers with independent status, last attempt, observation age, auth method and error code. HTTP success means the local service responds; it does not establish that every provider has live quota. A provider failure keeps only explicitly stale last-known observations, hides current percentages and retries automatically. Authentication renewal is performed in each provider's official app; this dashboard does not refresh or overwrite credentials.

All collectors are built into Headroom. CodexBar is not an installation or runtime dependency. The optional `--probe` diagnostic checks the built-in adapters in isolated temporary storage and reports sanitized statuses without restarting the service. See [provider collectors](providers.md) for native credential sources and compatibility with existing configuration.

Muse is integrated directly. The collector reads the existing CLI-owned OAuth credential from the macOS Keychain item `ai.meta.dev.credentials` / `meta`, then POSTs an empty body to `https://api.meta.ai/muse-code/key` with `x-api-version: 1.0.0`. This matches the installed Muse Code 1.4.2 protocol. Its response has explicit coding quota and the active tier; the returned key, payment details and profile fields are never persisted. Live checks confirmed that repeated requests returned the existing key and that the original plan key remained valid. No extension, browser cookie extraction or model inference is required.

If Muse OAuth expires, use `muse login` and Meta's normal approval screen, then Refresh Muse connection in Settings. macOS may require you to approve access to a renewed Keychain item. Background reads have a five-second deadline; a timed-out permission request is latched until an explicit refresh, preventing repeated credential dialogs. Headroom never asks for your password or changes Keychain ACLs. Provider 429 responses trigger bounded backoff; expired data is withheld during that wait.

The private data directory contains quota history, settings, a HMAC identity salt, sanitized Muse tier proof and native status. Back it up as a unit so identities and history remain consistent. Use SQLite's backup API or a clean service shutdown for a consistent backup; copying a WAL database without its journal can lose recent observations. Preserve the original model ledger and all historical `work/` receipts.

## Floating desktop widget

Install or update the native companion with `python3 scripts/install_menubar.py`. The installer builds and checks the new executable before replacing the owned companion. The floating widget appears by default on first launch, remains above ordinary app windows across Spaces, and can be dragged by its background.

Hide or show the widget from the Headroom menu. Hiding it keeps the menu indicator and collectors running. Opening `Headroom.app` again also restores a hidden widget. The widget's dashboard button opens the local plan overview at `http://127.0.0.1:8793/`. Only window position and visibility are saved as UI preferences; provider replies and credentials are not saved there.

The widget uses the menu companion's existing poll and expiry timer. A failed connection, expired quota, or passed reset withdraws the affected current percentage. A remaining fresh allowance can still be shown independently. It does not request additional provider credentials or create another background collector.

## Rollback

To stop the new dashboard while preserving all data:

```sh
launchctl bootout gui/$(id -u)/com.xxx.aiusage
```

Remove its plist only if it should no longer start at login. To restore the previous menu companion, boot out `com.xxx.codex-usage-menu`, copy `~/.local/share/aiusage/menubar-agent.before.plist` back to its original LaunchAgents filename and bootstrap it. The previous Codex Usage.app and its ledger remain intact. No hosted CI, provider subscription changes or purchases are part of installation or recovery.

## Verification boundary

All six provider collectors are integrated. Installation is local to each user's machine; cloning the repository does not install a service or establish authenticated provider coverage. Fresh coverage varies during upstream rate limits or session failures; the dashboard never presents retained percentages as current. Local tests establish the tested implementation. Provider outages and upstream schema changes remain operational dependencies.
