from datetime import timedelta

import pytest

from app.application.use_cases import conciliar_pagamentos
from app.domain.ports.provedor_pagamento import SituacaoPagamento
from tests.apoio import AGORA, orcamento_aprovado, orcamento_gerado, pagamento_pendente, publicadas


def evento(bd, tipo) -> list[dict]:
    return [c for c in publicadas(bd) if c["tipo"] == tipo]


def estado_do_pagamento(bd) -> str:
    return bd["pagamentos"].find_one({})["estado"]


def pagar(servicos, pagamento, situacao):
    servicos.provedor.definir_situacao(pagamento["provedor_ref"], situacao)


def test_pagamento_pago_publica_confirmacao(servicos, bd):
    envelope, pagamento = pagamento_pendente(servicos, bd)
    pagar(servicos, pagamento, SituacaoPagamento.PAGO)
    servicos.conciliacao.executar(AGORA + timedelta(minutes=1))
    assert estado_do_pagamento(bd) == "CONFIRMADO"
    [confirmado] = evento(bd, "PagamentoConfirmado")
    assert confirmado["payload"] == {"pagamento_id": pagamento["_id"], "valor": "220.00", "pago_em": "2026-11-12T15:01:00Z"}
    assert (confirmado["os_id"], confirmado["saga_id"]) == (envelope["os_id"], envelope["saga_id"])


def test_pagamento_recusado_publica_recusa(servicos, bd):
    _, pagamento = pagamento_pendente(servicos, bd)
    pagar(servicos, pagamento, SituacaoPagamento.RECUSADO)
    servicos.conciliacao.executar(AGORA)
    assert estado_do_pagamento(bd) == "RECUSADO"
    assert evento(bd, "PagamentoRecusado")[0]["payload"] == {"pagamento_id": pagamento["_id"], "motivo": "PAGAMENTO_RECUSADO"}


def test_qr_expirado_no_provedor_publica_expiracao(servicos, bd):
    _, pagamento = pagamento_pendente(servicos, bd)
    pagar(servicos, pagamento, SituacaoPagamento.EXPIRADO)
    servicos.conciliacao.executar(AGORA)
    assert estado_do_pagamento(bd) == "EXPIRADO"
    assert evento(bd, "PagamentoRecusado")[0]["payload"]["motivo"] == "PAGAMENTO_EXPIRADO"


def test_prazo_esgotado_cancela_no_provedor_e_publica_expiracao(servicos, bd):
    _, pagamento = pagamento_pendente(servicos, bd)
    servicos.conciliacao.executar(AGORA + timedelta(minutes=15))
    assert estado_do_pagamento(bd) == "EXPIRADO"
    assert servicos.provedor.consultar(pagamento["provedor_ref"]) == SituacaoPagamento.CANCELADO
    assert evento(bd, "PagamentoRecusado")[0]["payload"]["motivo"] == "PAGAMENTO_EXPIRADO"


def test_prazo_esgotado_com_pagamento_no_ultimo_instante_confirma(servicos, bd, monkeypatch):
    _, pagamento = pagamento_pendente(servicos, bd)
    monkeypatch.setattr(servicos.provedor, "cancelar", lambda ref: SituacaoPagamento.PAGO)
    servicos.conciliacao.executar(AGORA + timedelta(minutes=15))
    assert estado_do_pagamento(bd) == "CONFIRMADO" and evento(bd, "PagamentoConfirmado")
    assert not evento(bd, "PagamentoRecusado")


def test_pendente_dentro_do_prazo_nao_muda(servicos, bd):
    pagamento_pendente(servicos, bd)
    servicos.conciliacao.executar(AGORA + timedelta(minutes=14))
    assert estado_do_pagamento(bd) == "PENDENTE"
    assert not evento(bd, "PagamentoConfirmado") and not evento(bd, "PagamentoRecusado")


def test_pagamento_cancelado_no_provedor_encerra_sem_evento(servicos, bd):
    _, pagamento = pagamento_pendente(servicos, bd)
    pagar(servicos, pagamento, SituacaoPagamento.CANCELADO)
    servicos.conciliacao.executar(AGORA)
    assert estado_do_pagamento(bd) == "CANCELADO"
    assert not evento(bd, "PagamentoConfirmado") and not evento(bd, "PagamentoRecusado")


def test_reembolso_feito_direto_no_provedor_nao_e_tratado_aqui(servicos, bd):
    _, pagamento = pagamento_pendente(servicos, bd)
    pagar(servicos, pagamento, SituacaoPagamento.REEMBOLSADO)
    servicos.conciliacao.executar(AGORA)
    assert estado_do_pagamento(bd) == "PENDENTE"


def test_pagamento_ja_encerrado_nao_consulta_o_provedor(servicos, bd, monkeypatch):
    _, pagamento = pagamento_pendente(servicos, bd)
    pagar(servicos, pagamento, SituacaoPagamento.PAGO)
    servicos.conciliacao.executar(AGORA)
    monkeypatch.setattr(servicos.provedor, "consultar", lambda ref: pytest.fail("não deveria consultar"))
    servicos.conciliacao.executar(AGORA)
    assert len(evento(bd, "PagamentoConfirmado")) == 1


def test_orcamento_vencido_expira_e_publica_rejeicao(servicos, bd):
    envelope, gerado = orcamento_gerado(servicos, bd)
    servicos.conciliacao.executar(AGORA + timedelta(hours=24))
    assert bd["orcamentos"].find_one({})["estado"] == "EXPIRADO"
    [rejeitado] = evento(bd, "OrcamentoRejeitado")
    assert rejeitado["payload"] == {"orcamento_id": gerado["orcamento_id"], "motivo": "ORCAMENTO_EXPIRADO"}
    assert (rejeitado["os_id"], rejeitado["saga_id"]) == (envelope["os_id"], envelope["saga_id"])
    servicos.conciliacao.executar(AGORA + timedelta(hours=25))
    assert len(evento(bd, "OrcamentoRejeitado")) == 1


def test_orcamento_no_prazo_ou_ja_aprovado_nao_expira(servicos, bd):
    orcamento_gerado(servicos, bd)
    orcamento_aprovado(servicos, bd)
    servicos.conciliacao.executar(AGORA + timedelta(hours=23))
    servicos.conciliacao.executar(AGORA + timedelta(days=3))
    assert sorted(o["estado"] for o in bd["orcamentos"].find()) == ["APROVADO", "EXPIRADO"]


def test_duas_replicas_nao_publicam_o_mesmo_pagamento_duas_vezes(servicos, bd):
    _, pagamento = pagamento_pendente(servicos, bd)
    pagar(servicos, pagamento, SituacaoPagamento.PAGO)
    replica_a = servicos.banco.leitura().pagamentos.buscar_por_orcamento(pagamento["orcamento_id"])
    replica_b = servicos.banco.leitura().pagamentos.buscar_por_orcamento(pagamento["orcamento_id"])
    assert servicos.conciliacao.conciliar(replica_a, AGORA) is True
    assert servicos.conciliacao.conciliar(replica_b, AGORA) is False
    assert len(evento(bd, "PagamentoConfirmado")) == 1


def test_duas_replicas_nao_expiram_o_mesmo_orcamento_duas_vezes(servicos, bd):
    envelope, gerado = orcamento_gerado(servicos, bd)
    replica_a = servicos.banco.leitura().orcamentos.buscar(gerado["orcamento_id"])
    replica_b = servicos.banco.leitura().orcamentos.buscar(gerado["orcamento_id"])
    depois = AGORA + timedelta(hours=24)
    assert servicos.conciliacao._expirar_orcamento(replica_a, depois) is True
    assert servicos.conciliacao._expirar_orcamento(replica_b, depois) is False
    assert len(evento(bd, "OrcamentoRejeitado")) == 1


def test_falha_em_um_pagamento_nao_impede_os_demais(servicos, bd, monkeypatch, caplog):
    pagamentos = [pagamento_pendente(servicos, bd)[1] for _ in range(2)]
    for pagamento in pagamentos:
        pagar(servicos, pagamento, SituacaoPagamento.PAGO)
    consultar = servicos.provedor.consultar
    falhar_na_primeira = iter([True])

    def instavel(ref):
        if next(falhar_na_primeira, False):
            raise ConnectionError("provedor fora do ar")
        return consultar(ref)
    monkeypatch.setattr(servicos.provedor, "consultar", instavel)
    servicos.conciliacao.executar(AGORA)
    assert sorted(p["estado"] for p in bd["pagamentos"].find()) == ["CONFIRMADO", "PENDENTE"]
    assert any(r.evento == "conciliacao_falhou" for r in caplog.records if hasattr(r, "evento"))


def test_conciliacao_percorre_todos_os_lotes(servicos, bd, monkeypatch):
    monkeypatch.setattr(conciliar_pagamentos, "LOTE", 2)
    for _ in range(5):
        pagar(servicos, pagamento_pendente(servicos, bd)[1], SituacaoPagamento.PAGO)
    pagamento_pendente(servicos, bd)
    servicos.conciliacao.executar(AGORA)
    assert sorted(p["estado"] for p in bd["pagamentos"].find()) == ["CONFIRMADO"] * 5 + ["PENDENTE"]


def test_notificacao_do_provedor_consulta_a_situacao_real(servicos, bd):
    _, pagamento = pagamento_pendente(servicos, bd)
    assert servicos.notificacao_mercadopago.executar(pagamento["provedor_ref"], AGORA) is False
    pagar(servicos, pagamento, SituacaoPagamento.PAGO)
    assert servicos.notificacao_mercadopago.executar(pagamento["provedor_ref"], AGORA) is True
    assert estado_do_pagamento(bd) == "CONFIRMADO"
    assert servicos.notificacao_mercadopago.executar(pagamento["provedor_ref"], AGORA) is False
    assert len(evento(bd, "PagamentoConfirmado")) == 1


def test_notificacao_de_pedido_desconhecido_e_ignorada(servicos, caplog):
    assert servicos.notificacao_mercadopago.executar("ORD-inexistente") is False
    assert any(getattr(r, "evento", "") == "notificacao_sem_pagamento" for r in caplog.records)
