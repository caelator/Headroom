# Headroom

Local-first dashboard for verified AI subscription quotas and consumption across Codex, Muse, z.ai, Cursor, Antigravity and Claude.

Run local checks only. Do not add, enable, or trigger hosted CI for pushes, pull requests or schedules; do not push without checking hosted triggers. Preserve existing user data and credentials. Never log or persist auth tokens, cookies or raw account responses. Use scripts/diagnostics.py for runtime checks; never print raw launchd environments or credential metadata. Provider-reported quotas, local token counters, manual inputs and estimates are different evidence classes. Percent remaining must come from provider percentages or a known allowance in the same units/window. Forecasts must not bridge resets, account changes, or stale gaps.

The selected Codex model manages; delegated workers use native Muse Wonder 1.0 (muse-wonder-1.0) agents with medium reasoning. Specify both settings on every spawn; no automatic fallback or recursive delegation. Assign disjoint, bounded files; manager owns integration and validation. A worker report alone does not establish completion. Preserve the personal CI policy above.
