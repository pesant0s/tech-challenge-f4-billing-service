"""Adapter do Mercado Pago contra respostas simuladas (sem rede) e o webhook com assinatura."""
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
import respx

from app.adapters.outbound.pagamento.mercadopago import ProvedorMercadoPago
from app.domain.ports.provedor_pagamento import FalhaNoProvedor, SituacaoPagamento
from app.infrastructure.config import settings
from tests.apoio import AGORA, pagamento_pendente, publicadas

URL = "https://mp.teste"


@pytest.fixture
def mp():
    with respx.mock(base_url=URL, assert_all_called=False) as roteador:
        yield ProvedorMercadoPago(URL, "TOKEN-DE-TESTE", "oficina-caixa-1"), roteador


def test_criar_cobranca_envia_o_pedido_do_qr_dinamico(mp):
    provedor, roteador = mp
    rota = roteador.post("/v1/orders").respond(201, json={"id": "ORD01", "type_response": {"qr_data": "000201PIX"}})
    cobranca = provedor.criar_cobranca("pag-1", "os-1", Decimal("220"), "x" * 200, datetime.now(timezone.utc) + timedelta(minutes=15))
    assert (cobranca.provedor_ref, cobranca.qr_data) == ("ORD01", "000201PIX")
    pedido = rota.calls.last.request
    assert pedido.headers["authorization"] == "Bearer TOKEN-DE-TESTE" and pedido.headers["x-idempotency-key"] == "pag-1"
    assert json.loads(pedido.content) == {
        "type": "qr", "external_reference": "os-1", "description": "x" * 150, "total_amount": "220.00", "expiration_time": "PT15M",
        "config": {"qr": {"external_pos_id": "oficina-caixa-1", "mode": "dynamic"}},
        "transactions": {"payments": [{"amount": "220.00"}]},
        "items": [{"title": "x" * 150, "unit_price": "220.00", "quantity": 1, "unit_measure": "unit"}],
    }


def test_criar_cobranca_aceita_qr_data_na_raiz(mp):
    provedor, roteador = mp
    roteador.post("/v1/orders").respond(201, json={"id": "ORD02", "qr_data": "000201RAIZ"})
    assert provedor.criar_cobranca("p", "o", Decimal("1"), "d", datetime.now(timezone.utc)).qr_data == "000201RAIZ"


def test_criar_cobranca_com_erro_do_provedor_levanta(mp):
    provedor, roteador = mp
    roteador.post("/v1/orders").respond(500)
    with pytest.raises(httpx.HTTPStatusError):
        provedor.criar_cobranca("p", "o", Decimal("1"), "d", AGORA)


@pytest.mark.parametrize("pedido, esperado", [
    ({"status": "processed"}, SituacaoPagamento.PAGO),
    ({"status": "canceled"}, SituacaoPagamento.CANCELADO),
    ({"status": "expired"}, SituacaoPagamento.EXPIRADO),
    ({"status": "refunded"}, SituacaoPagamento.REEMBOLSADO),
    ({"status": "failed"}, SituacaoPagamento.RECUSADO),
    ({"status": "action_required", "transactions": {"payments": [{"status": "rejected"}]}}, SituacaoPagamento.RECUSADO),
    ({"status": "action_required", "transactions": {"payments": [{"status": "failed"}]}}, SituacaoPagamento.RECUSADO),
    ({"status": "action_required", "transactions": {"payments": [{"status": "created"}]}}, SituacaoPagamento.PENDENTE),
    ({"status": "created"}, SituacaoPagamento.PENDENTE),
    ({"status": "created", "transactions": None}, SituacaoPagamento.PENDENTE),
])
def test_consultar_mapeia_a_situacao_do_pedido(mp, pedido, esperado):
    provedor, roteador = mp
    rota = roteador.get("/v1/orders/ORD01").respond(200, json=pedido)
    assert provedor.consultar("ORD01") == esperado
    assert rota.called


def test_cancelar_devolve_a_situacao_final(mp):
    provedor, roteador = mp
    rota = roteador.post("/v1/orders/ORD01/cancel").respond(200, json={"status": "canceled"})
    assert provedor.cancelar("ORD01") == SituacaoPagamento.CANCELADO
    # Sem a chave, o Mercado Pago recusa o cancelamento com 400.
    assert rota.calls.last.request.headers["x-idempotency-key"] == "cancelar-ORD01"


def test_cancelar_qr_vencido_devolve_expirado(mp):
    provedor, roteador = mp
    roteador.post("/v1/orders/ORD01/cancel").respond(409, json={"errors": [{"code": "cannot_cancel_order"}]})
    roteador.get("/v1/orders/ORD01").respond(200, json={"status": "expired"})
    assert provedor.cancelar("ORD01") == SituacaoPagamento.EXPIRADO


def test_cancelar_com_conflito_consulta_a_situacao_atual(mp):
    provedor, roteador = mp
    roteador.post("/v1/orders/ORD01/cancel").respond(409, json={"message": "already paid"})
    roteador.get("/v1/orders/ORD01").respond(200, json={"status": "processed"})
    assert provedor.cancelar("ORD01") == SituacaoPagamento.PAGO


def test_cancelar_com_erro_inesperado_levanta(mp):
    provedor, roteador = mp
    roteador.post("/v1/orders/ORD01/cancel").respond(503)
    with pytest.raises(httpx.HTTPStatusError):
        provedor.cancelar("ORD01")


def test_reembolsar_envia_a_chave_de_idempotencia(mp):
    provedor, roteador = mp
    rota = roteador.post("/v1/orders/ORD01/refund").respond(200, json={})
    provedor.reembolsar("ORD01", "reembolso:pag-1")
    assert rota.calls.last.request.headers["x-idempotency-key"] == "reembolso:pag-1"


def test_falha_no_reembolso_leva_o_corpo_resumido(mp):
    provedor, roteador = mp
    roteador.post("/v1/orders/ORD01/refund").respond(400, text="y" * 500)
    with pytest.raises(FalhaNoProvedor) as erro:
        provedor.reembolsar("ORD01", "reembolso:pag-1")
    assert erro.value.detalhe == "HTTP 400: " + "y" * 200


SEGREDO = "segredo-do-webhook"


def assinado(data_id: str, request_id="req-1", ts="1700000000", segredo=SEGREDO) -> dict:
    manifesto = f"id:{data_id.lower() if data_id.isalnum() else data_id};request-id:{request_id};ts:{ts};"
    v1 = hmac.new(segredo.encode(), manifesto.encode(), hashlib.sha256).hexdigest()
    return {"x-signature": f"ts={ts},v1={v1}", "x-request-id": request_id}


@pytest.fixture
def com_segredo(monkeypatch):
    monkeypatch.setattr(settings, "MP_WEBHOOK_SECRET", SEGREDO)


def notificar(client, data_id, cabecalhos, tipo="order"):
    return client.post("/billing/webhooks/mercadopago", params={"data.id": data_id}, headers=cabecalhos,
                       json={"type": tipo, "action": "order.processed", "data": {"id": data_id}})


def test_webhook_assinado_concilia_o_pagamento(client, servicos, bd, com_segredo):
    _, pagamento = pagamento_pendente(servicos, bd, datetime.now(timezone.utc))
    servicos.provedor.definir_situacao(pagamento["provedor_ref"], SituacaoPagamento.PAGO)
    resposta = notificar(client, pagamento["provedor_ref"], assinado(pagamento["provedor_ref"]))
    assert resposta.status_code == 200 and resposta.json() == {"processado": True}
    assert bd["pagamentos"].find_one({})["estado"] == "CONFIRMADO"
    assert publicadas(bd)[-1]["tipo"] == "PagamentoConfirmado"


def test_webhook_nao_confia_no_corpo_e_consulta_o_provedor(client, servicos, bd, com_segredo):
    _, pagamento = pagamento_pendente(servicos, bd, datetime.now(timezone.utc))
    resposta = notificar(client, pagamento["provedor_ref"], assinado(pagamento["provedor_ref"]))
    assert resposta.json() == {"processado": False}
    assert bd["pagamentos"].find_one({})["estado"] == "PENDENTE"


def test_webhook_com_id_alfanumerico_assina_em_minusculas(client, com_segredo):
    resposta = notificar(client, "ORD01ABC", assinado("ORD01ABC"))
    assert resposta.status_code == 200 and resposta.json() == {"processado": False}


def test_webhook_usa_o_id_do_corpo_quando_a_query_nao_traz(client, com_segredo):
    manifesto = "request-id:req-1;ts:1;"
    v1 = hmac.new(SEGREDO.encode(), manifesto.encode(), hashlib.sha256).hexdigest()
    resposta = client.post("/billing/webhooks/mercadopago", headers={"x-signature": f"ts=1,v1={v1}", "x-request-id": "req-1"},
                           json={"type": "order", "data": {"id": "ORD9"}})
    assert resposta.status_code == 200 and resposta.json() == {"processado": False}


@pytest.mark.parametrize("cabecalhos", [
    assinado("ORD1", segredo="outro-segredo"),
    {**assinado("ORD1"), "x-request-id": "adulterado"},
    {"x-signature": "ts=1700000000", "x-request-id": "req-1"},
    {"x-signature": "lixo"},
    {},
], ids=["segredo errado", "request-id adulterado", "sem v1", "formato inválido", "sem assinatura"])
def test_webhook_com_assinatura_invalida_responde_401(client, servicos, bd, com_segredo, cabecalhos):
    _, pagamento = pagamento_pendente(servicos, bd, datetime.now(timezone.utc))
    servicos.provedor.definir_situacao(pagamento["provedor_ref"], SituacaoPagamento.PAGO)
    assert notificar(client, "ORD1", cabecalhos).status_code == 401
    assert bd["pagamentos"].find_one({})["estado"] == "PENDENTE"


def test_webhook_sem_segredo_configurado_responde_401(client, monkeypatch):
    monkeypatch.setattr(settings, "MP_WEBHOOK_SECRET", None)
    assert notificar(client, "ORD1", assinado("ORD1", segredo="")).status_code == 401


def test_webhook_de_outro_topico_responde_200_sem_efeito(client, servicos, bd, com_segredo):
    _, pagamento = pagamento_pendente(servicos, bd, datetime.now(timezone.utc))
    servicos.provedor.definir_situacao(pagamento["provedor_ref"], SituacaoPagamento.PAGO)
    resposta = notificar(client, pagamento["provedor_ref"], assinado(pagamento["provedor_ref"]), tipo="payment")
    assert resposta.status_code == 200 and resposta.json() == {"processado": False}
    assert bd["pagamentos"].find_one({})["estado"] == "PENDENTE"


def test_webhook_nao_exige_jwt(client, com_segredo):
    assert notificar(client, str(uuid.uuid4()), {}).status_code == 401  # recusado pela assinatura, não pelo token
