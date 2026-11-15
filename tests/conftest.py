import os
import uuid
from datetime import datetime, timedelta, timezone

MONGO_URL_TESTE = os.environ.get("MONGO_URL_TESTE", "mongodb://localhost:27017/?replicaSet=rs0")
os.environ.setdefault("MONGO_URL", MONGO_URL_TESTE)
os.environ.setdefault("SECRET_KEY", "chave-de-teste-com-mais-de-32-caracteres")

import pytest
from fastapi.testclient import TestClient
from jose import jwt
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from app.adapters.inbound.http.dependencias import obter_servicos
from app.adapters.outbound.pagamento.simulado import ProvedorSimulado
from app.adapters.outbound.persistence.banco import Banco
from app.infrastructure.composicao import Servicos
from app.infrastructure.config import settings
from app.main import app


@pytest.fixture(scope="session")
def cliente_mongo():
    cliente = MongoClient(MONGO_URL_TESTE, tz_aware=True, serverSelectionTimeoutMS=2000)
    try:
        cliente.admin.command("ping")
    except PyMongoError:
        pytest.exit(f"MongoDB em replica set indisponível em {MONGO_URL_TESTE}: suba o `docker compose up mongo` "
                    "ou defina MONGO_URL_TESTE (os testes usam transações, que exigem replica set).", returncode=1)
    yield cliente
    cliente.close()


@pytest.fixture
def bd(cliente_mongo):
    """Banco com nome aleatório, apagado no fim do teste."""
    nome = f"billing_teste_{uuid.uuid4().hex}"
    yield cliente_mongo[nome]
    cliente_mongo.drop_database(nome)


@pytest.fixture
def banco(cliente_mongo, bd):
    banco = Banco(cliente_mongo, bd)
    banco.preparar_indices()
    return banco


@pytest.fixture
def servicos(banco, bd):
    return Servicos(banco, ProvedorSimulado(bd), timedelta(minutes=1440), timedelta(minutes=15))


@pytest.fixture
def client(servicos):
    app.dependency_overrides[obter_servicos] = lambda: servicos
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture(scope="session")
def emitir_token():
    """Token no formato do OS Service (funcionário) ou da Lambda (cliente); os claims podem ser sobrescritos."""
    def _emitir(minutos: int = 60, **claims) -> dict:
        corpo = {"sub": "ana", "role": "ATENDENTE", "tipo": "usuario",
                 "exp": datetime.now(timezone.utc) + timedelta(minutes=minutos), **claims}
        token = jwt.encode({k: v for k, v in corpo.items() if v is not None}, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
        return {"Authorization": f"Bearer {token}"}
    return _emitir


@pytest.fixture
def atendente_headers(emitir_token):
    return emitir_token(sub="atendente", role="ATENDENTE")
