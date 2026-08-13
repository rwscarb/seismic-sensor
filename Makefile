.PHONY: build dev down logs restart deploy deploy-static deploy-clean data fixtures train lint shell

include .env
export

build:
	docker compose build --no-cache

dev:
	docker compose -f docker-compose.yml -f docker-compose.dev.yml up

dev-mock:
	MOCK=1 MOCK_EVENT_INTERVAL_S=30 docker compose -f docker-compose.yml -f docker-compose.dev.yml up

down:
	docker compose down

logs:
	docker compose logs -f

restart:
	docker compose restart

# Fly.io
deploy: ## Smart deploy — static/template-only changes push via sftp (no restart); else full fly deploy
	@CHANGED=$$(git diff HEAD --name-only; git diff --cached --name-only); \
	if [ -z "$$CHANGED" ]; then \
		echo "Nothing staged or modified vs HEAD — running full deploy anyway"; \
		fly deploy; \
	elif echo "$$CHANGED" | grep -qvE '^seismic/(static|templates)/'; then \
		echo "Non-static files changed — full deploy"; \
		fly deploy; \
	else \
		echo "Static/template files only — uploading directly (no restart)..."; \
		tar czf - seismic/static/app.js seismic/static/app.css seismic/templates/index.html \
			| fly ssh console -C "tar xzf - -C /app"; \
		echo "Done. Changes live without restart."; \
	fi

deploy-static: ## Force-push static/template files to running container (no restart)
	tar czf - seismic/static/app.js seismic/static/app.css seismic/templates/index.html \
		| fly ssh console -C "tar xzf - -C /app"
	@echo "Static files deployed (no restart)."

deploy-clean:
	fly deploy --no-cache

fly-logs:
	fly logs

fly-env:
	@grep -v '^#' .env | xargs fly secrets set

data: ## Pull labeled training data from Fly volume → ./training/
	mkdir -p training
	fly ssh console -C "tar czf - /data/training" | tar xzf - --strip-components=2 -C training/

fixtures: ## Pull detections.json from Fly → ./fixtures/ for local dev pre-seeding
	mkdir -p fixtures
	fly sftp get /data/detections.json fixtures/detections.json

train: data ## Fine-tune StreamingNet on labeled data in ./training/
	uv run python train.py --data training/ --checkpoints checkpoints/ $(TRAIN_ARGS)

lint: ## Run ruff check
	uv run ruff check .

shell:
	docker compose exec seismic-sensor bash
