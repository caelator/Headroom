# Local release qualification

Headroom uses local gates only. Do not add or trigger hosted jobs for pushes, pull requests or schedules. Check repository Actions settings and committed workflow triggers before publication. No notarized downloadable binary is currently published; installation compiles and ad-hoc signs the app locally.

## Candidate gate

```sh
make docs PYTHON=python3
make release-check PYTHON=python3
```

`release-check` runs Python tests, HTTP/storage/privacy/forecast boundaries, Python compilation, JavaScript checks and README/preview checks. It then compiles the native app with the actual macOS 13 deployment target, runs its deterministic self-test and shared selection fixtures, and decodes a synthetic loopback response. The source fingerprint must stay unchanged throughout.

The private `work/release-validation/<fingerprint>/gate.json` receipt identifies tested source and build. It explicitly does not claim live provider or installation qualification. Keep the receipt; do not publish private runtime data.

## Installation and live qualification

Install the service, then native companion from exactly the qualified source without intervening edits. Run `scripts/diagnostics.py` and establish artifact, fingerprint, PID, port ownership and heartbeat agreement. Inspect each provider's fresh/partial/auth/rate-limit status independently; an unavailable upstream provider must withdraw current numbers rather than prevent the dashboard from opening.

Record the original salt and database preservation locally without exposing their contents. Verify restart recovery by restarting the owned LaunchAgents, observing new PIDs, and confirming the same installed fingerprint and coherent history. Test that a bound-port collision prevents new collection, a collection deadline cannot stall peers, and shutdown releases owned workers and the port. Synthetic tests cover failure paths and installer rollback; installed restart evidence is still necessary.

On an unlocked desktop, verify the widget is visible and reachable, draggable, does not take focus, hides without stopping the service, reopens, and opens the intended dashboard. A `panel_visible` heartbeat alone is not visual proof. Check overview/history/provider navigation and settings in the real browser; use synthetic data for any published screenshots.

## Public page

Commit the candidate with the local hooks active. Pre-commit generates README facts from the staged index. Pre-push verifies the exact pushed objects and the inspected synthetic preview receipt. After pushing `main`, verify that the public repository's default branch and README match the candidate. Other branch pushes do not change GitHub's default-branch page.

Hooks are per clone and can be explicitly bypassed; contributors must activate them with `make hooks`. These boundaries are documented rather than presented as a server-enforced guarantee.
