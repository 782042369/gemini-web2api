# gemini-web2api development tasks.
# Production deployment still goes through docker-compose (see README).

PYTHON ?= python3
export PYTHONPATH := src

.PHONY: test lint run docker-build docker-up docker-down clean

## test: run the unit test suite (no network, upstream fully mocked)
test:
	$(PYTHON) -m unittest discover -v

## lint: static checks with ruff
lint:
	ruff check src tests

## run: start a dev server on :8081 from the source tree
run:
	$(PYTHON) -m gemini_web2api

## docker-build / docker-up / docker-down: container lifecycle
docker-build:
	docker-compose build

docker-up:
	docker-compose up -d

docker-down:
	docker-compose down

## clean: remove caches
clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov build dist *.egg-info
