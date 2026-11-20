.PHONY: test smoke up down
test smoke:
	./scripts/check.sh
up:
	docker compose up --build -d --wait
down:
	docker compose down -v --remove-orphans
