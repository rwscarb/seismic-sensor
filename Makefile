.PHONY: build dev down logs restart deploy data train shell

include .env
export

build:
	docker compose build --no-cache

dev:
	docker compose up

dev-mock:
	MOCK=1 MOCK_EVENT_INTERVAL_S=30 docker compose up

down:
	docker compose down

logs:
	docker compose logs -f

restart:
	docker compose restart

# Fly.io
deploy:
	fly deploy

deploy-clean:
	fly deploy --no-cache

fly-logs:
	fly logs

fly-env:
	@grep -v '^#' .env | xargs fly secrets set

data: ## Pull labeled training data from Fly volume → ./training/
	mkdir -p training
	fly ssh console -C "tar czf - /data/training" | tar xzf - --strip-components=2 -C training/

train: data ## Fine-tune StreamingNet on labeled data in ./training/
	uv run python train.py --data training/ --checkpoints checkpoints/ $(TRAIN_ARGS)

shell:
	docker compose exec seismic-sensor bash
