test:
	python -m pytest tests/ -v

dev:
	python server.py

.PHONY: test dev
