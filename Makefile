# Makefile for Ingals

.PHONY: build up down logs restart clean shell-backend shell-frontend

# Build and start containers
up:
	docker-compose up -d --build

# Build containers without starting
build:
	docker-compose build

# Stop containers
down:
	docker-compose down

# View logs
logs:
	docker-compose logs -f

# Restart containers
restart: down up

# Clean up (remove volumes and orphans)
clean:
	docker-compose down -v --remove-orphans

# Access backend shell
shell-backend:
	docker-compose exec backend /bin/bash

# Access frontend shell
shell-frontend:
	docker-compose exec frontend /bin/sh
