PYTHON ?= /opt/homebrew/opt/python@3.11/libexec/bin/python3

.PHONY: run check install
run:
	PYTHONPATH=src $(PYTHON) -m aiusage.server

check:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v
	$(PYTHON) -m compileall -q src
	node --check web/app.js
	node tests/test_web.js

install:
	$(PYTHON) scripts/install.py
