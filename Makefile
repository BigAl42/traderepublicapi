all: help

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'


fmt: ## Formats everything
	black .

check: ## Syntax-check Python sources and run offline tests
	python3 -m py_compile trapi/api.py trapi/__init__.py setup.py
	python3 -m py_compile examples/*.py LS/convert-stammdaten.py tests/*.py
	python3 -m py_compile tr-adapter/*.py mcp_server.py smoke_mcp.py
	python3 -m unittest discover -s tests -t .

test: ## Run offline unit tests (no Trade Republic account)
	python3 -m unittest discover -s tests -t . -v
	python3 tr-adapter/test_mcp_server.py -v

smoke: ## MCP stdio plumbing smoke (no Trade Republic account)
	python3 smoke_mcp.py

