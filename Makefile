.PHONY: build dev down logs restart deploy train shell

include .env
export

build:
	docker compose build --no-cache

dev:
	docker compose up

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

train:
	python train.py --out checkpoints/ $(TRAIN_ARGS)

shell:
	docker compose exec seismic-sensor bash
