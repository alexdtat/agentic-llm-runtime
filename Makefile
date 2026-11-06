PYTHON ?= python3

.PHONY: test smoke
test:
	$(PYTHON) checkpoint_checks.py A

smoke:
	$(PYTHON) checkpoint_checks.py A --infrastructure
