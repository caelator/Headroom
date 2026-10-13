#!/usr/bin/env python3
"""Install the immutable local Headroom service, preserving previous versions."""
from install_service import install
from install_support import InstallError

if __name__=='__main__':
    try:install()
    except (InstallError,OSError,ValueError):raise SystemExit('Service installation failed; previous artifacts are preserved. Run diagnostics before retrying.')
