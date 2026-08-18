all: help

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'


fmt: ## Formats everything
	black .

check: ## Syntax-check Python sources
	python3 -m py_compile trapi/api.py trapi/__init__.py setup.py
	python3 -m py_compile examples/*.py LS/convert-stammdaten.py

