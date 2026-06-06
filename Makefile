dev:
	python server.py

daemon:
	python daemon.py

daemon-status:
	@curl -sf http://127.0.0.1:8912/health && echo

.PHONY: dev daemon daemon-status
