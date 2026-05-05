test:
	python -m pytest tests/ -v

dev:
	python server.py

daemon:
	python daemon.py

daemon-status:
	@curl -sf http://127.0.0.1:8912/health && echo

.PHONY: test dev daemon daemon-status
