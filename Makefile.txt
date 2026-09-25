# Makefile
# `make demo` is the single-command path your master prompt asks for as an
# alternative/companion to `docker compose up` - useful for a live demo
# where you want to see terminal output scroll (parser fallback warnings,
# request logs) rather than a detached container.
#
# NOTE: recipe lines below use real tab characters, not spaces - `make`
# requires this and silently fails/misparses otherwise. Verified with a
# byte-level check before delivery.

.PHONY: demo test install clean docker-up docker-down

install:
	pip install -r requirements.txt

demo: install
	uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

test: install
	pytest tests/ -v

docker-up:
	docker compose up --build

docker-down:
	docker compose down

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache data/events.db
