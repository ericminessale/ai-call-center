# SignalWire Call Center - Makefile
.PHONY: help setup test up down logs clean install-deps quick-test

# Default target
help:
	@echo "SignalWire Call Center - Available Commands"
	@echo "==========================================="
	@echo ""
	@echo "Quick Start:"
	@echo "  make setup          - Initial setup (install deps, copy files)"
	@echo "  make up             - Start all services with Docker"
	@echo "  make test           - Run quick test of AI agents"
	@echo ""
	@echo "Docker Commands:"
	@echo "  make up             - Start all services"
	@echo "  make down           - Stop all services"
	@echo "  make logs           - View logs (all services)"
	@echo "  make logs-agents    - View AI agent logs"
	@echo "  make logs-backend   - View backend logs"
	@echo "  make restart        - Restart all services"
	@echo "  make clean          - Stop and remove all containers/volumes"
	@echo ""
	@echo "Development:"
	@echo "  make install-deps   - Install Python dependencies locally"
	@echo "  make quick-test     - Run quick test script"
	@echo "  make test-agents    - Test AI agents with swaig-test"
	@echo "  make db-upgrade     - Run database migrations"
	@echo "  make db-reset       - Reset database (WARNING: destroys data)"
	@echo ""
	@echo "Utilities:"
	@echo "  make shell-backend  - Shell into backend container"
	@echo "  make shell-agents   - Shell into agents container"
	@echo "  make shell-db       - Connect to PostgreSQL"
	@echo ""

# Initial setup
setup:
	@echo "Setting up SignalWire Call Center..."
	@echo "1. Checking for .env file..."
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		echo "   Created .env from template - PLEASE EDIT WITH YOUR CREDENTIALS"; \
	else \
		echo "   .env already exists"; \
	fi
	@echo ""
	@echo "2. Copying backend files from transcribe-app..."
	@if [ -d "../transcribe-app/app" ]; then \
		cp -r ../transcribe-app/app backend/ 2>/dev/null || echo "   Backend files already copied"; \
		cp ../transcribe-app/requirements.txt backend/ 2>/dev/null || true; \
		cp ../transcribe-app/wsgi.py backend/ 2>/dev/null || true; \
		echo "   Backend files copied"; \
	else \
		echo "   WARNING: transcribe-app not found - backend may be incomplete"; \
	fi
	@echo ""
	@echo "3. Creating directories..."
	@mkdir -p backend/migrations
	@mkdir -p frontend/src
	@mkdir -p ai-agents/knowledge
	@echo "   Directories created"
	@echo ""
	@echo "Setup complete! Next steps:"
	@echo "  1. Edit .env with your SignalWire credentials"
	@echo "  2. Run 'make up' to start services"
	@echo "  3. Run 'make test' to verify AI agents work"

# Start services
up:
	docker-compose up -d --build
	@echo ""
	@echo "Services starting..."
	@sleep 5
	docker-compose ps
	@echo ""
	@echo "Services available at:"
	@echo "  - AI Agents:  http://localhost:8080"
	@echo "  - Backend:    http://localhost:5000"
	@echo "  - Frontend:   http://localhost:3000"
	@echo ""
	@echo "Run 'make logs' to view output"

# Stop services
down:
	docker-compose down

# View logs
logs:
	docker-compose logs -f

logs-agents:
	docker-compose logs -f ai-agents

logs-backend:
	docker-compose logs -f backend

logs-frontend:
	docker-compose logs -f frontend

# Restart services
restart:
	docker-compose restart

# Clean everything
clean:
	docker-compose down -v
	@echo "All containers and volumes removed"

# Install dependencies locally
install-deps:
	@echo "Installing AI agents dependencies..."
	cd ai-agents && pip install -r requirements.txt
	@echo ""
	@echo "Installing backend dependencies..."
	cd backend && pip install -r requirements.txt
	@echo ""
	@echo "Dependencies installed!"

# Quick test
quick-test:
	python quick_test.py

# Test agents with swaig-test
test-agents:
	@command -v swaig-test >/dev/null 2>&1 || { echo "Installing swaig-test..."; pip install signalwire-agents; }
	swaig-test ai-agents/main_agent.py --list-tools

# Database operations
db-upgrade:
	docker-compose exec backend flask db upgrade

db-reset:
	@echo "WARNING: This will delete all data!"
	@read -p "Are you sure? (y/N) " -n 1 -r; \
	echo ""; \
	if [[ $$REPLY =~ ^[Yy]$$ ]]; then \
		docker-compose down -v; \
		docker-compose up -d postgres; \
		sleep 5; \
		docker-compose exec backend flask db upgrade; \
		echo "Database reset complete"; \
	fi

# Shell access
shell-backend:
	docker-compose exec backend /bin/bash

shell-agents:
	docker-compose exec ai-agents /bin/bash

shell-db:
	docker-compose exec postgres psql -U ccuser -d callcenter

# Test individual agents
test-receptionist:
	@echo "Testing Basic Receptionist..."
	curl -X POST http://localhost:8080/receptionist \
		-H "Content-Type: application/json" \
		-d '{"message": "I need help with sales"}'

test-sales:
	@echo "Testing Sales Receptionist..."
	curl -X POST http://localhost:8080/sales \
		-H "Content-Type: application/json" \
		-d '{"message": "Hi, I want to learn about your products"}'

test-support:
	@echo "Testing Support Receptionist..."
	curl -X POST http://localhost:8080/support \
		-H "Content-Type: application/json" \
		-d '{"message": "I have a technical issue"}'

# Development shortcuts
dev-agents:
	cd ai-agents && python main_agent.py

dev-backend:
	cd backend && flask run

dev-frontend:
	cd frontend && npm run dev