.DEFAULT_GOAL := help
.PHONY: help setup up down logs sh venv test test-docker cov build render kubeconfig implantar

AWS_REGION ?= us-east-1
TAG        ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo latest)
VENV       := .venv
PYTHON     ?= $(shell command -v python3.12 2>/dev/null || echo python3)
# Delimitador `|` no sed: num Makefile, `#` abriria um comentário.
GITHUB_OWNER ?= $(shell git config --get remote.origin.url 2>/dev/null | sed -E 's|^.*github\.com[:/]([^/]+)/.*$$|\1|')
TESTE = MONGO_URL="mongodb://localhost:27017/?replicaSet=rs0" SECRET_KEY="chave-de-teste-com-mais-de-32-caracteres"

help: ## Lista os alvos
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Cria o .env a partir do .env.example, sem sobrescrever
	@test -f .env || cp .env.example .env

up: setup ## Sobe API, MongoDB e RabbitMQ com hot-reload (http://localhost:8003/docs)
	docker compose up --build

down: ## Derruba a stack local e remove os volumes
	docker compose down -v

logs: ## Acompanha os logs da API
	docker compose logs -f api

sh: ## Abre um shell no container da API
	docker compose exec api sh

venv: ## Cria o ambiente virtual (o projeto é testado em Python 3.12)
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/python -m pip install -q -r requirements.txt

test: ## Roda os testes (exige o MongoDB em replica set em localhost:27017)
	@test -x $(VENV)/bin/python || $(MAKE) --no-print-directory venv
	$(TESTE) $(VENV)/bin/python -m pytest -q

test-docker: setup ## Roda os testes dentro do container, sem Python na máquina
	docker compose build api
	docker compose run --rm -e MONGO_URL_TESTE="mongodb://mongo:27017/?directConnection=true" -e SECRET_KEY="chave-de-teste-com-mais-de-32-caracteres" api "python -m pytest -q -p no:cacheprovider"

cov: ## Testes com cobertura
	@test -x $(VENV)/bin/python || $(MAKE) --no-print-directory venv
	$(TESTE) $(VENV)/bin/python -m pytest --cov=app --cov-report=term-missing --cov-report=xml

build: ## Constrói a imagem localmente
	docker build -t billing-service:$(TAG) .

render: ## Renderiza os manifestos que irão para o cluster
	kubectl kustomize k8s/overlays/producao

kubeconfig: ## Aponta o kubectl para o cluster
	aws eks update-kubeconfig --name tech-challenge-eks --region $(AWS_REGION)

# O deploy passa sempre pelo pipeline, que publica a imagem e monta o Secret.
implantar: ## Aciona o pipeline de deploy na main
	@test -n "$(GITHUB_OWNER)" || { echo "Dono do repositório desconhecido: rode com GITHUB_OWNER=seu-usuario."; exit 1; }
	gh workflow run ci-cd.yml --ref main --repo $(GITHUB_OWNER)/tech-challenge-f4-billing-service
