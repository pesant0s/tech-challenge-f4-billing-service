import uuid

import pytest

from app.adapters.outbound.pagamento.simulado import ProvedorSimulado
from app.domain.exceptions import NotFoundException
from app.domain.ports.provedor_pagamento import SituacaoPagamento
from app.adapters.outbound.pagamento.mercadopago import ProvedorMercadoPago
from tests.apoio import AGORA, pagamento_pendente, publicadas


@pytest.fixture
def simulador(bd):
    return ProvedorSimulado(bd)


def criar(simulador, pagamento_id="pag-1"):
    return simulador.criar_cobranca(pagamento_id, "os-1", 100, "d", AGORA)


def test_cobranca_simulada_tem_qr_ficticio_e_situacao_pendente(simulador):
    cobranca = criar(simulador)
    assert cobranca.qr_data.startswith("00020101SIMULADO") and cobranca.provedor_ref == "pag-1" and cobranca.expira_em == AGORA
    assert simulador.consultar("pag-1") == SituacaoPagamento.PENDENTE and simulador.nome == "SIMULADO"


def test_repetir_a_cobranca_nao_reinicia_a_situacao(simulador):
    criar(simulador)
    simulador.definir_situacao("pag-1", SituacaoPagamento.PAGO)
    criar(simulador)
    assert simulador.consultar("pag-1") == SituacaoPagamento.PAGO


def test_cancelar_pendente_cancela(simulador):
    criar(simulador)
    assert simulador.cancelar("pag-1") == SituacaoPagamento.CANCELADO


def test_cancelar_respeita_o_pagamento_ja_feito(simulador):
    criar(simulador)
    simulador.definir_situacao("pag-1", SituacaoPagamento.PAGO)
    assert simulador.cancelar("pag-1") == SituacaoPagamento.PAGO


def test_reembolsar_marca_o_pedido_como_reembolsado(simulador):
    criar(simulador)
    simulador.reembolsar("pag-1", "reembolso:pag-1")
    assert simulador.consultar("pag-1") == SituacaoPagamento.REEMBOLSADO


def test_situacao_de_cobranca_desconhecida_e_recusada(simulador):
    with pytest.raises(NotFoundException):
        simulador.definir_situacao("nao-existe", SituacaoPagamento.PAGO)


@pytest.mark.parametrize("acao, situacao", [
    ("aprovar", SituacaoPagamento.PAGO), ("recusar", SituacaoPagamento.RECUSADO), ("expirar", SituacaoPagamento.EXPIRADO),
])
def test_rota_do_simulador_altera_a_situacao_e_a_conciliacao_publica(client, servicos, bd, atendente_headers, acao, situacao):
    _, pagamento = pagamento_pendente(servicos, bd)
    resposta = client.post(f"/billing/simulador/pagamentos/{pagamento['_id']}/{acao}", headers=atendente_headers)
    assert resposta.status_code == 204
    assert servicos.provedor.consultar(pagamento["provedor_ref"]) == situacao
    assert bd["pagamentos"].find_one({})["estado"] == "PENDENTE"
    servicos.conciliacao.executar(AGORA)
    assert publicadas(bd)[-1]["tipo"] in ("PagamentoConfirmado", "PagamentoRecusado")


def test_simulador_responde_404_para_pagamento_desconhecido(client, atendente_headers):
    assert client.post(f"/billing/simulador/pagamentos/{uuid.uuid4()}/aprovar", headers=atendente_headers).status_code == 404


def test_simulador_exige_token_de_funcionario(client, emitir_token):
    url = f"/billing/simulador/pagamentos/{uuid.uuid4()}/aprovar"
    assert client.post(url).status_code == 401
    assert client.post(url, headers=emitir_token(sub=str(uuid.uuid4()), tipo="cliente", role=None)).status_code == 403


def test_acao_desconhecida_do_simulador_e_rejeitada(client, atendente_headers):
    assert client.post(f"/billing/simulador/pagamentos/{uuid.uuid4()}/estornar", headers=atendente_headers).status_code == 422


def test_simulador_nao_existe_com_o_mercado_pago(client, servicos, atendente_headers, monkeypatch):
    monkeypatch.setattr(servicos, "provedor", ProvedorMercadoPago("https://mp.teste", "t", "pos"))
    resposta = client.post(f"/billing/simulador/pagamentos/{uuid.uuid4()}/aprovar", headers=atendente_headers)
    assert resposta.status_code == 404
