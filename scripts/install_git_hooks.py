#!/usr/bin/env python3
"""Activate repository-local documentation hooks without changing global Git config."""
from pathlib import Path
import subprocess
import sys

root=Path(__file__).resolve().parents[1]
existing=subprocess.run(['git','config','--local','--get','core.hooksPath'],cwd=root,capture_output=True,text=True)
if existing.returncode not in (0,1):raise SystemExit('Could not inspect local Git hooks configuration.')
if existing.stdout.strip() not in ('','.githooks'):raise SystemExit('An existing hooksPath is configured; preserve and chain those hooks before activating Headroom hooks.')
directory=Path(subprocess.check_output(['git','rev-parse','--git-path','hooks'],cwd=root,text=True).strip())
if not directory.is_absolute():directory=root/directory
if not existing.stdout.strip() and directory.exists():
    custom=[p for p in directory.iterdir() if p.is_file() and not p.name.endswith('.sample')]
    if custom:raise SystemExit('Existing custom Git hooks were found; preserve and chain them before activating Headroom hooks.')
for name in ('pre-commit','pre-push'):
    hook=root/'.githooks'/name
    hook.chmod(hook.stat().st_mode|0o111)
subprocess.run(['git','config','--local','core.hooksPath','.githooks'],cwd=root,check=True)
print('Installed local README refresh and push verification hooks.')
