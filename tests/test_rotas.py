"""Rotas do orçamento: quem pode ver e decidir, e o que cada decisão publica."""
import uuid
from datetime import datetime, timezone

import pytest
from jose import jwt

from app.domain.exceptions import ConflictException
from tests.apoio import AGORA, cobrar, novo_cliente, orcamento_gerado, publicadas


def agora():
    return datetime.now(timezone.utc)


@pytest.fixture
def cliente():
    return novo_cliente()


@pytest.fixture
def orcamento(servicos, bd, cliente):
    return orcamento_gerado(servicos, bd, cliente, agora())[0]


def token_de_cliente(emitir_token, cliente_id):
    return emitir_token(sub=cliente_id, tipo="cliente", role=None)


def test_funcionario_ve_o_orcamento_sem_pagamento(client, orcamento, atendente_headers):
    resposta = client.get(f"/billing/orcamentos/{orcamento['os_id']}", headers=atendente_headers)
    assert resposta.status_code == 200
    corpo = resposta.json()
    assert (corpo["estado"], corpo["valor_total"], corpo["os_id"], corpo["pagamento"]) == ("GERADO", "220.00", orcamento["os_id"], None)
    assert corpo["cliente"]["nome"] == "Ana Souza" and len(corpo["itens"]) == 2


def test_cliente_titular_ve_o_orcamento_com_o_pagamento(client, servicos, bd, emitir_token):
    cliente = novo_cliente()
    envelope, gerado = orcamento_gerado(servicos, bd, cliente, agora())
    servicos.decisao.aprovar(envelope["os_id"], "CLIENTE", None, agora())
    servicos.comandos.executar(cobrar(envelope["os_id"], envelope["saga_id"], gerado["orcamento_id"]), agora())
    corpo = client.get(f"/billing/orcamentos/{envelope['os_id']}", headers=token_de_cliente(emitir_token, cliente["id"])).json()
    assert corpo["estado"] == "APROVADO" and corpo["aprovado_por"] == "CLIENTE"
    pagamento = corpo["pagamento"]
    assert pagamento["estado"] == "PENDENTE" and pagamento["valor"] == "220.00" and pagamento["qr_data"].startswith("00020101SIMULADO")
    assert pagamento["provedor"] == "SIMULADO" and pagamento["expira_em"] and pagamento["pago_em"] is None


def test_cliente_nao_ve_o_orcamento_de_outro(client, orcamento, emitir_token):
    resposta = client.get(f"/billing/orcamentos/{orcamento['os_id']}", headers=token_de_cliente(emitir_token, str(uuid.uuid4())))
    assert resposta.status_code == 404 and resposta.json()["detail"] == "Orçamento não encontrado"


def test_orcamento_inexistente_responde_404_com_correlation_id(client, atendente_headers):
    resposta = client.get(f"/billing/orcamentos/{uuid.uuid4()}", headers=atendente_headers)
    assert resposta.status_code == 404 and resposta.json()["correlation_id"]


def test_os_com_id_invalido_responde_422(client, atendente_headers):
    assert client.get("/billing/orcamentos/abc", headers=atendente_headers).status_code == 422


def test_cliente_titular_aprova(client, servicos, bd, orcamento, cliente, emitir_token):
    resposta = client.post(f"/billing/orcamentos/{orcamento['os_id']}/aprovar", headers=token_de_cliente(emitir_token, cliente["id"]))
    assert resposta.status_code == 200 and resposta.json()["estado"] == "APROVADO" and resposta.json()["aprovado_por"] == "CLIENTE"
    aprovado = publicadas(bd)[-1]
    assert aprovado["tipo"] == "OrcamentoAprovado" and aprovado["payload"]["aprovado_por"] == "CLIENTE"
    assert (aprovado["os_id"], aprovado["saga_id"]) == (orcamento["os_id"], orcamento["saga_id"])


def test_funcionario_aprova(client, bd, orcamento, atendente_headers):
    resposta = client.post(f"/billing/orcamentos/{orcamento['os_id']}/aprovar", headers=atendente_headers)
    assert resposta.json()["aprovado_por"] == "FUNCIONARIO"
    assert publicadas(bd)[-1]["payload"]["aprovado_por"] == "FUNCIONARIO"


def test_outro_cliente_nao_aprova_nem_rejeita(client, bd, orcamento, emitir_token):
    intruso = token_de_cliente(emitir_token, str(uuid.uuid4()))
    assert client.post(f"/billing/orcamentos/{orcamento['os_id']}/aprovar", headers=intruso).status_code == 404
    assert client.post(f"/billing/orcamentos/{orcamento['os_id']}/rejeitar", headers=intruso).status_code == 404
    assert bd["orcamentos"].find_one({})["estado"] == "GERADO" and len(publicadas(bd)) == 1


def test_orcamento_vencido_nao_e_aprovado(client, servicos, bd, cliente, atendente_headers):
    envelope, _ = orcamento_gerado(servicos, bd, cliente, AGORA)
    resposta = client.post(f"/billing/orcamentos/{envelope['os_id']}/aprovar", headers=atendente_headers)
    assert resposta.status_code == 422 and resposta.json()["detail"] == "Orçamento expirado"
    assert bd["orcamentos"].find_one({})["estado"] == "GERADO"


def test_cliente_titular_rejeita(client, bd, orcamento, cliente, emitir_token):
    resposta = client.post(f"/billing/orcamentos/{orcamento['os_id']}/rejeitar", headers=token_de_cliente(emitir_token, cliente["id"]))
    assert resposta.status_code == 200 and resposta.json()["estado"] == "REJEITADO"
    assert publicadas(bd)[-1]["payload"] == {"orcamento_id": bd["orcamentos"].find_one({})["_id"], "motivo": "ORCAMENTO_REJEITADO"}


def test_orcamento_decidido_nao_pode_ser_decidido_de_novo(client, orcamento, atendente_headers):
    url = f"/billing/orcamentos/{orcamento['os_id']}"
    assert client.post(f"{url}/rejeitar", headers=atendente_headers).status_code == 200
    assert client.post(f"{url}/aprovar", headers=atendente_headers).status_code == 422
    assert client.post(f"{url}/rejeitar", headers=atendente_headers).status_code == 422


def test_conflito_de_estado_responde_409(client, servicos, orcamento, atendente_headers, monkeypatch):
    def corrida(*_):
        raise ConflictException("O estado mudou antes da gravação")
    monkeypatch.setattr(servicos.decisao, "aprovar", corrida)
    assert client.post(f"/billing/orcamentos/{orcamento['os_id']}/aprovar", headers=atendente_headers).status_code == 409


def test_sem_token_responde_401(client, orcamento):
    url = f"/billing/orcamentos/{orcamento['os_id']}"
    assert client.get(url).status_code == 401
    assert client.post(f"{url}/aprovar").status_code == 401
    assert client.post(f"{url}/rejeitar").status_code == 401


@pytest.mark.parametrize("claims", [
    {"minutos": -5},
    {"role": "VISITANTE"},
    {"role": None},
    {"tipo": "outro"},
    {"sub": None},
    {"sub": None, "tipo": "cliente", "role": None},
], ids=["expirado", "papel desconhecido", "sem papel", "tipo desconhecido", "sem sub", "cliente sem sub"])
def test_token_invalido_responde_401(client, orcamento, emitir_token, claims):
    assert client.get(f"/billing/orcamentos/{orcamento['os_id']}", headers=emitir_token(**claims)).status_code == 401


def test_token_com_outra_chave_responde_401(client, orcamento):
    token = jwt.encode({"sub": "ana", "tipo": "usuario", "role": "ADMIN"}, "outra-chave", algorithm="HS256")
    assert client.get(f"/billing/orcamentos/{orcamento['os_id']}", headers={"Authorization": f"Bearer {token}"}).status_code == 401
