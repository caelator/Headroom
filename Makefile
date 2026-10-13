PYTHON ?= /opt/homebrew/opt/python@3.11/libexec/bin/python3

.PHONY: run check install docs hooks preview-receipt
run:
	PYTHONPATH=src $(PYTHON) -m aiusage.server

check:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v
	$(PYTHON) -m compileall -q src
	node --check web/app.js
	node tests/test_web.js
	$(PYTHON) scripts/github_docs.py --check

docs:
	$(PYTHON) scripts/github_docs.py --write

hooks:
	$(PYTHON) scripts/install_git_hooks.py

preview-receipt:
	$(PYTHON) scripts/github_docs.py --record-preview

install:
	$(PYTHON) scripts/install.py
