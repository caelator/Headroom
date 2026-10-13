# Contributing to Headroom

Headroom is a local-first monitor. Changes should make quota and consumption evidence easier to understand while preserving provider credentials and local history.

## Development setup

Use Python 3.11+ and Node.js. The Python runtime has no third-party dependencies. Provider connections and the native menu companion target macOS; synthetic unit tests do not require live provider credentials.

```sh
git clone https://github.com/caelator/Headroom.git
cd Headroom
make check PYTHON=python3
make run PYTHON=python3
```

The Makefile defaults to Homebrew's Python 3.11 path. Override `PYTHON` for another installation. The dashboard opens at [127.0.0.1:8793](http://127.0.0.1:8793/). See [operations](docs/operations.md) for installation and recovery.

## Source layout

| Path | Responsibility |
| --- | --- |
| `src/aiusage/` | Provider adapters, sanitized storage, analytics, local receipt accounting, and the HTTP service. |
| `web/` | Dashboard markup, styles, and browser behavior. |
| `native/MenuBar.swift` | Native macOS menu companion. |
| `scripts/` | Installation and allowlisted runtime diagnostics. |
| `tests/` | Synthetic provider fixtures, temporary databases, HTTP boundary checks, and JavaScript checks. |
| `docs/` | Measurement contracts and operational guidance. |

## Changes and validation

Keep each change focused on one behavior. Describe the trigger, expected result, and local verification in your pull request. Run `make check PYTHON=python3` before submitting. It runs Python unit tests, Python compilation, JavaScript syntax validation, and dashboard regression checks.

For native changes, compile into a temporary directory and run the built companion with `--check`. Building requires Xcode command-line tools. Do not run the installer just to validate a source change: it replaces the local companion and registers a LaunchAgent.

Use local gates only. Do not add, enable, or trigger hosted CI for pushes, pull requests, or schedules, and do not require hosted merge checks. Check for hosted triggers before pushing. Existing tag-triggered artifact build/sign workflows are the only policy exception; this repository currently has none.

## Provider and measurement contracts

- Keep provider quota, local token counters, declared values, and forecasts as distinct evidence classes.
- Derive percentages only from provider percentages or a known allowance in matching units and window.
- Keep account identity, allowance identity, and reset cycle continuity separate. Forecasts must stop across resets, changed accounts, and stale gaps.
- Invalidate current values on expiry or collection failure; keep historical values explicitly labelled.
- Use synthetic, minimal fixtures. Never commit credentials, cookies, conversation text, raw account responses, or private runtime databases.
- Preserve existing data, HMAC identity salts, receipt ledgers, and historical records. Tests must use isolated temporary data.

Read [measurement contracts](docs/metrics.md) before changing normalization, accounting, or forecasts. Use `python3 scripts/diagnostics.py` for runtime checks; do not print raw launchd environments or credential metadata.

Report ordinary problems through the [bug form](https://github.com/caelator/Headroom/issues/new?template=bug_report.yml). Follow the [security policy](SECURITY.md) for vulnerabilities.
