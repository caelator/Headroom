# Keeping the GitHub page current

GitHub renders the README, documentation, images, and repository files from the pushed default branch. Headroom adds local Git hooks so public source facts cannot drift from the revision being published. No hosted job or scheduled automation is involved.

## Activate in a clone

```sh
make hooks PYTHON=python3
```

The hooks are already active in the original development checkout. Git intentionally does not activate cloned hooks automatically, so contributors run this once in their own clone. The installer changes only this repository's `core.hooksPath`. It refuses to overwrite existing custom hooks; those must be preserved and chained.

If your Python 3.11+ executable has another name, set `HEADROOM_PYTHON` in the environment used for Git. Ordinary commits and pushes then run the hooks automatically.

## What happens on commit and push

The pre-commit hook refreshes the README's marked source blocks from the **staged index**. These blocks describe package version, Python/macOS requirements, provider catalog and implementation links, polling/freshness constants, and the native floating-widget capability. It stages the generated README with the source change. Unstaged source changes are excluded, and an independently edited working README is preserved byte for byte.

The pre-push hook reads every ref Git actually intends to publish, peels annotated tags, and checks each exact committed revision. A correct working README cannot hide a stale pushed commit. Branch deletions do not need a document snapshot. A push is blocked when generated facts are stale, linked files are missing, source/asset paths are symbolic links, or the synthetic dashboard preview no longer matches the frontend source receipt. Hosted workflows require policy review; push/PR/schedule CI remains prohibited.

Manually written explanations remain reviewable prose. Generated blocks are owned by `scripts/github_docs.py`; update the source or generator rather than editing those blocks by hand. The fingerprint binds the listed source contents and is not a test or production-qualification claim.

## Refresh documents and previews

```sh
make docs PYTHON=python3
make check PYTHON=python3
```

For frontend changes, render the actual dashboard using synthetic fixtures, inspect the new preview, and replace `docs/assets/dashboard.jpg`. Then record its source binding:

```sh
make preview-receipt PYTHON=python3
```

Stage the changed source, README, image, and receipt together and commit normally. The receipt records only source/image hashes; it never includes live account data. Do not re-record an old image to clear the check without verifying that it depicts the new frontend.

The checks can be run explicitly against a candidate commit:

```sh
python3 scripts/github_docs.py --check --revision HEAD
```

These are repository-local safeguards. Git's explicit hook-bypass options and unconfigured clones can bypass them; normal contributing workflows should keep the hooks active.
