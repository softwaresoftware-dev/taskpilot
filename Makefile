test:
	python -m pytest tests/ -v

# Real-claude end-to-end integration (L3). Spawns an actual agent: needs
# `claude` on PATH and the taskpilot daemon + session-bridge running. Uses
# tokens and ~60s wall clock, so it's opt-in and excluded from `make test`.
test-e2e:
	TASKPILOT_E2E=1 python -m pytest tests/test_e2e_real_agent.py -v -m e2e

dev:
	python server.py

daemon:
	python daemon.py

daemon-status:
	@curl -sf http://127.0.0.1:8912/health && echo

.PHONY: test dev daemon daemon-status
