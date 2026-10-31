# Headroom

Local-first dashboard for verified AI subscription quotas and consumption.

Run applicable local checks. Do not add, enable, or trigger hosted PR, push, or scheduled CI, or require hosted merge checks. Existing tag-triggered artifact releases are the exception. Inspect triggers before pushing.

- Preserve user data and credentials. Never log or persist tokens, cookies, raw account responses, launchd environments, or credential metadata. Use `scripts/diagnostics.py` for runtime checks.
- Keep provider quotas, local counters, manual inputs and estimates distinguishable. Percent remaining requires a provider percentage or a known allowance in matching units and window. Forecasts must not bridge resets, account changes or stale gaps.
- Follow the current personal delegation policy; do not duplicate model defaults here.
- For changes affecting the public GitHub page, run `make docs` and relevant local gates. Its README blocks come from `scripts/github_docs.py`. Use `make hooks` when installing the repository hooks, preserving custom hooks; pre-commit checks the index and pre-push checks the exact pushed objects.
- For frontend changes, regenerate and visually inspect the synthetic dashboard preview and record its source/image hashes with `make preview-receipt`. Never publish live account screenshots or reuse a preview receipt without inspection. See [the page workflow](docs/github-page.md).
