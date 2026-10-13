# Operations and recovery

Headroom is a per-user macOS application. The dashboard service uses LaunchAgent `com.xxx.aiusage`; the owned menu companion remains `com.xxx.codex-usage-menu`. The app is `~/Applications/Headroom.app`, with unique bundle identifier `com.caelator.headroom`. Other legacy apps and the existing model ledger are preserved.

## Install and update

Run the local release gate, then install both components from the same unchanged source:

```sh
make hooks PYTHON=python3
make release-check PYTHON=python3
make install PYTHON=python3
make install-native PYTHON=python3
python3 scripts/diagnostics.py
```

The service executes a code-only payload under `~/.local/share/aiusage/releases/<source fingerprint>/`, verified against its manifest and made read-only. It keeps the existing private data directory. Editable checkout changes cannot silently change the running service. Earlier payloads remain available for recovery.

The native companion is compiled for the current architecture with an actual macOS 13 deployment target, self-tested, checked against the matching service and ad-hoc signed before replacing the live bundle. The installers use a kernel lock, private atomic metadata writes and a persistent transaction journal. Previous LaunchAgent inodes, bundles and installation receipts are preserved before activation. Activation and rollback register the active app path with macOS LaunchServices and unregister preserved inactive copies, so reopening cannot select a failed or archived build. All preserved bundles remain on disk. A failed activation rolls back that component. Service and native installations are separate transactions; diagnostics detect a mismatched pair if one component cannot be updated.

## Runtime checks

```sh
python3 scripts/diagnostics.py
python3 scripts/diagnostics.py --probe
launchctl kickstart -k gui/$(id -u)/com.xxx.aiusage
```

Diagnostics print allowlisted status fields. Installed checks bind the payload manifest, launchd PID, loopback listener owner, service fingerprint, app bundle and binary hashes, and native heartbeat. `consistent` establishes their agreement; it does not prove that every provider is online or that the widget is unobscured. `panel_visible` is only the panel's own visibility flag. Never share raw launchd environments or complete private logs.

HTTP success means the local service responds. Six provider entries with independent status do not imply six fresh measurements. Each collector runs in its own persistent process with a 45-second absolute deadline. A timed-out collector is terminated, backs off, and cannot block the other providers. Manual refresh respects rate-limit backoff. The HTTP listener has bounded connections and absolute request-read deadlines.

A provider or authentication failure immediately withdraws current percentages and forecasts; explicitly labelled last-known history remains. Provider sessions are renewed in their official applications. Muse and Claude Keychain reads have a five-second deadline; a timed-out approval request stays latched until an explicit Refresh now, preventing repeated dialogs. Headroom does not request passwords or change Keychain access controls. See [provider collectors](providers.md).

## Data preservation and backup

The private directory contains `plans.sqlite3`, its WAL, settings, the original HMAC identity salt, sanitized tier proof, installation receipts and native status. Back it up as a unit. Use SQLite's backup API or a clean service shutdown; copying a WAL database without its journal can lose recent observations. Preserve the existing local model ledger at `~/.codex/model-usage/data/usage.sqlite3` and historical `work/` receipts.

The most recent 93 days of quota observations and alerts stay in the active database. Hourly maintenance copies older rows in bounded batches to `archives/YYYY-MM.sqlite3`, verifies the committed copies, then removes only those copied rows from active storage. A crash between copy and removal is retried idempotently. CSV export combines active and archived observations and omits account identities. The interactive history range remains at most 90 days.

Cold history is preserved indefinitely, so total storage grows. Include archives in backups and monitor free disk space. Failed archival leaves active rows available for retry. A corrupt or incompatible archive makes full export fail instead of returning silently incomplete history; preserve that archive and restore a verified backup. Do not delete it merely to clear the error.

Damaged salts and unsupported database schemas fail closed without inventing new identities. Malformed provider/settings records are retained under quarantine keys inside the original database; valid settings survive fieldwise repair. Restore a damaged database and its original salt together.

## Floating desktop widget

`make install-native` reveals the widget once after that installation. Its position and later visibility preference are remembered. Drag its background to move it. The full panel is clamped onto an available screen and adjusts after display-layout changes. It stays above ordinary windows across Spaces without activating the app.

Hide/show it from the Headroom menu. Hiding leaves the menu and collectors running. Opening `Headroom.app` again reveals a hidden widget. Open dashboard checks the matching local service before opening `http://127.0.0.1:8793/` and reports an actionable connection failure locally.

The widget shows every provider-reported quota window in labelled tiles. A 5-hour allowance and a weekly allowance have their own percentages, meters and reset countdowns. Extra model pools stay distinct; scroll to reach every window. Only windows actually returned by the provider appear. Hover a tile to inspect the source, reported window length and exact reset time.

Check now requests collection through the matching local service and clears credential-approval latches. It respects upstream retry delays, and the confirmation says the check was queued rather than implying that fresh data has already arrived. The menu also offers Check quotas now.

The widget uses the existing poll and expiry timer. Numeric percentages and explicit unlimited readings require current observations; stale values are labelled Last, with no historical meter or forecast. A passed reset is shown explicitly, and unlimited is never synthesized as 100%. Separate plan percentages are never averaged.

## Recovery and rollback

The next installer run recovers an interrupted `pending-install.json` transaction while holding the kernel lock. If the app, LaunchAgent or preserved backup changed externally, recovery stops and retains the evidence for review. Do not delete the journal or backups to force activation.

`installation.json` records each component's fingerprint and backup paths. LaunchAgent backups live under `~/Library/LaunchAgents/.headroom-backups/`; app backups under `~/Applications/.headroom-backups/`; previous receipts under the private data directory. Restore a selected component with its matching plist and bundle after booting out its LaunchAgent. Keep service and native source fingerprints aligned. Reinstalling a prior source revision also leaves history intact.

To stop the service without deleting data:

```sh
launchctl bootout gui/$(id -u)/com.xxx.aiusage
```

Boot out `com.xxx.codex-usage-menu` to stop the companion. Remove a plist only if it should no longer start at login. No hosted CI or provider subscription changes are part of installation or recovery.

## Verification boundary

Synthetic tests qualify tested behavior; source-bound release gates qualify that exact code and native build. Installed diagnostics qualify local artifact/process identity. Actual restart recovery, provider coverage and desktop interaction are separate checks. Upstream outages and schema changes remain operational dependencies. See [release qualification](release.md) for the complete procedure.
