"""Os quatro comandos do Billing: caso feliz, idempotência, mensagem repetida e compensações."""
import uuid
from datetime import timedelta

import pytest

from app.domain.entities.pagamento import EstadoPagamento
from app.domain.exceptions import ConflictException
from app.domain.ports.provedor_pagamento import FalhaNoProvedor, SituacaoPagamento
from tests.apoio import (AGORA, cancelar_orcamento, cobrar, gerar_orcamento, orcamento_aprovado, orcamento_gerado,
                         pagamento_pendente, publicadas, reembolsar)


def tipos(bd) -> list[str]:
    return [c["tipo"] for c in publicadas(bd)]


def test_gerar_orcamento_cria_e_publica(servicos, bd, caplog):
    os_id, saga_id = uuid.uuid4(), uuid.uuid4()
    with caplog.at_level("INFO", logger="oficina.notificacoes"):
        assert servicos.comandos.executar(gerar_orcamento(os_id, saga_id), AGORA) is True
    orcamento = bd["orcamentos"].find_one({"os_id": str(os_id)})
    assert orcamento["estado"] == "GERADO" and orcamento["valor_total"] == "220.00"
    [evento] = publicadas(bd)
    assert (evento["tipo"], evento["saga_id"], evento["os_id"]) == ("OrcamentoGerado", str(saga_id), str(os_id))
    assert evento["payload"] == {"orcamento_id": orcamento["_id"], "valor_total": "220.00", "expira_em": "2026-11-13T15:00:00Z"}
    envio = next(r for r in caplog.records if r.evento == "orcamento_enviado")
    assert (envio.os_id, envio.destinatario, envio.valor_total) == (str(os_id), "ana@example.com", "220.00")


def test_notificacao_cai_para_o_telefone_sem_email(servicos, bd, caplog):
    cliente = {"id": str(uuid.uuid4()), "nome": "Ana", "email": None, "telefone": "11999990000"}
    with caplog.at_level("INFO", logger="oficina.notificacoes"):
        servicos.comandos.executar(gerar_orcamento(uuid.uuid4(), uuid.uuid4(), cliente), AGORA)
    assert next(r for r in caplog.records if r.evento == "orcamento_enviado").destinatario == "11999990000"


def test_gerar_orcamento_de_outra_mensagem_para_a_mesma_os_republica_sem_duplicar(servicos, bd, caplog):
    os_id, saga_id = uuid.uuid4(), uuid.uuid4()
    servicos.comandos.executar(gerar_orcamento(os_id, saga_id), AGORA)
    caplog.clear()
    with caplog.at_level("INFO", logger="oficina.notificacoes"):
        servicos.comandos.executar(gerar_orcamento(os_id, saga_id), AGORA + timedelta(minutes=1))
    assert bd["orcamentos"].count_documents({}) == 1
    primeiro, segundo = publicadas(bd)
    assert primeiro["payload"] == segundo["payload"] and primeiro["message_id"] != segundo["message_id"]
    assert not [r for r in caplog.records if r.name == "oficina.notificacoes"]


def test_mensagem_repetida_nao_tem_efeito(servicos, bd):
    envelope = gerar_orcamento(uuid.uuid4(), uuid.uuid4())
    assert servicos.comandos.executar(envelope, AGORA) is True
    assert servicos.comandos.executar(envelope, AGORA) is False
    assert len(publicadas(bd)) == 1 and bd["mensagens_processadas"].count_documents({}) == 1


def test_cobrar_orcamento_aprovado_cria_o_pagamento(servicos, bd):
    envelope, orcamento_id = orcamento_aprovado(servicos, bd)
    servicos.comandos.executar(cobrar(envelope["os_id"], envelope["saga_id"], orcamento_id), AGORA)
    pagamento = bd["pagamentos"].find_one({})
    assert pagamento["estado"] == "PENDENTE" and pagamento["provedor"] == "SIMULADO" and pagamento["valor"] == "220.00"
    assert pagamento["qr_data"].startswith("00020101SIMULADO")
    assert pagamento["expira_em"] == AGORA + timedelta(minutes=15)
    cobranca = publicadas(bd)[-1]
    assert cobranca["tipo"] == "CobrancaCriada" and cobranca["os_id"] == envelope["os_id"]
    assert cobranca["payload"] == {"pagamento_id": pagamento["_id"], "provedor": "SIMULADO", "valor": "220.00",
                                   "qr_data": pagamento["qr_data"], "expira_em": "2026-11-12T15:15:00Z"}


def test_cobrar_de_novo_republica_a_mesma_cobranca(servicos, bd):
    envelope, pagamento = pagamento_pendente(servicos, bd)
    servicos.comandos.executar(cobrar(envelope["os_id"], envelope["saga_id"], pagamento["orcamento_id"]), AGORA)
    cobrancas = [c for c in publicadas(bd) if c["tipo"] == "CobrancaCriada"]
    assert len(cobrancas) == 2 and cobrancas[0]["payload"] == cobrancas[1]["payload"]
    assert bd["pagamentos"].count_documents({}) == 1 and bd["simulador_pagamentos"].count_documents({}) == 1


def test_pagamento_tem_id_derivado_do_orcamento(servicos, bd):
    _, pagamento = pagamento_pendente(servicos, bd)
    assert pagamento["_id"] == str(uuid.uuid5(uuid.NAMESPACE_URL, f"pagamento:{pagamento['orcamento_id']}"))


@pytest.mark.parametrize("estado", ["inexistente", "GERADO"])
def test_cobrar_sem_orcamento_aprovado_recusa(servicos, bd, estado):
    os_id, saga_id = uuid.uuid4(), uuid.uuid4()
    orcamento_id = str(uuid.uuid4())
    if estado == "GERADO":
        servicos.comandos.executar(gerar_orcamento(os_id, saga_id), AGORA)
        orcamento_id = publicadas(bd)[0]["payload"]["orcamento_id"]
    servicos.comandos.executar(cobrar(os_id, saga_id, orcamento_id), AGORA)
    assert publicadas(bd)[-1]["payload"] == {"pagamento_id": "", "motivo": "PAGAMENTO_RECUSADO"}
    assert bd["pagamentos"].count_documents({}) == 0


def test_cobrar_nao_chama_o_provedor_dentro_da_transacao(servicos, bd, monkeypatch):
    envelope, orcamento_id = orcamento_aprovado(servicos, bd)
    original = servicos.banco.transacao
    abertas = []
    monkeypatch.setattr(servicos.banco, "transacao", lambda: (abertas.append(1), original())[1])
    criar = servicos.provedor.criar_cobranca
    monkeypatch.setattr(servicos.provedor, "criar_cobranca", lambda *a, **k: (abertas.append("provedor") or criar(*a, **k)))
    servicos.comandos.executar(cobrar(envelope["os_id"], envelope["saga_id"], orcamento_id), AGORA)
    assert abertas == ["provedor", 1]


def test_cancelar_com_pagamento_pendente_cancela_tudo(servicos, bd):
    envelope, pagamento = pagamento_pendente(servicos, bd)
    servicos.comandos.executar(cancelar_orcamento(envelope["os_id"], envelope["saga_id"]), AGORA)
    assert bd["pagamentos"].find_one({})["estado"] == "CANCELADO"
    assert bd["orcamentos"].find_one({})["estado"] == "CANCELADO"
    assert servicos.provedor.consultar(pagamento["provedor_ref"]) == SituacaoPagamento.CANCELADO
    assert publicadas(bd)[-1]["payload"] == {"orcamento_id": pagamento["orcamento_id"]}
    assert tipos(bd)[-1] == "OrcamentoCancelado"


def test_cancelar_que_encontra_o_pagamento_ja_pago_confirma_para_a_saga_reembolsar(servicos, bd):
    envelope, pagamento = pagamento_pendente(servicos, bd)
    servicos.provedor.definir_situacao(pagamento["provedor_ref"], SituacaoPagamento.PAGO)
    servicos.comandos.executar(cancelar_orcamento(envelope["os_id"], envelope["saga_id"]), AGORA)
    assert bd["pagamentos"].find_one({})["estado"] == "CONFIRMADO"
    assert bd["orcamentos"].find_one({})["estado"] == "CANCELADO"
    confirmado, cancelado = publicadas(bd)[-2:]
    assert confirmado["tipo"] == "PagamentoConfirmado" and confirmado["payload"]["pagamento_id"] == pagamento["_id"]
    assert confirmado["payload"]["pago_em"] == "2026-11-12T15:00:00Z"
    assert cancelado["tipo"] == "OrcamentoCancelado"


def test_cancelar_orcamento_sem_pagamento(servicos, bd):
    envelope, _ = orcamento_gerado(servicos, bd)
    servicos.comandos.executar(cancelar_orcamento(envelope["os_id"], envelope["saga_id"]), AGORA)
    assert bd["orcamentos"].find_one({})["estado"] == "CANCELADO"
    assert tipos(bd)[-1] == "OrcamentoCancelado"


def test_cancelar_orcamento_rejeitado_nao_o_altera(servicos, bd):
    envelope, _ = orcamento_gerado(servicos, bd)
    servicos.decisao.rejeitar(envelope["os_id"], None, AGORA)
    servicos.comandos.executar(cancelar_orcamento(envelope["os_id"], envelope["saga_id"]), AGORA)
    assert bd["orcamentos"].find_one({})["estado"] == "REJEITADO"


def test_cancelar_sem_orcamento_publica_id_vazio(servicos, bd):
    servicos.comandos.executar(cancelar_orcamento(uuid.uuid4(), uuid.uuid4()), AGORA)
    assert publicadas(bd)[-1]["payload"] == {"orcamento_id": ""}


def test_cancelar_duas_vezes_publica_duas_vezes_sem_mudar_o_estado(servicos, bd):
    envelope, _ = pagamento_pendente(servicos, bd)
    for _ in range(2):
        servicos.comandos.executar(cancelar_orcamento(envelope["os_id"], envelope["saga_id"]), AGORA)
    assert tipos(bd).count("OrcamentoCancelado") == 2
    assert bd["pagamentos"].find_one({})["estado"] == "CANCELADO"


def confirmar(servicos, bd):
    envelope, pagamento = pagamento_pendente(servicos, bd)
    servicos.provedor.definir_situacao(pagamento["provedor_ref"], SituacaoPagamento.PAGO)
    servicos.conciliacao.executar(AGORA)
    return envelope, pagamento


def test_reembolsar_pagamento_confirmado(servicos, bd):
    envelope, pagamento = confirmar(servicos, bd)
    servicos.comandos.executar(reembolsar(envelope["os_id"], envelope["saga_id"]), AGORA)
    assert bd["pagamentos"].find_one({})["estado"] == "REEMBOLSADO"
    assert servicos.provedor.consultar(pagamento["provedor_ref"]) == SituacaoPagamento.REEMBOLSADO
    assert publicadas(bd)[-1]["payload"] == {"pagamento_id": pagamento["_id"], "valor": "220.00"}
    assert tipos(bd)[-1] == "PagamentoReembolsado"


def test_reembolsar_de_novo_republica_sem_chamar_o_provedor(servicos, bd, monkeypatch):
    envelope, pagamento = confirmar(servicos, bd)
    servicos.comandos.executar(reembolsar(envelope["os_id"], envelope["saga_id"]), AGORA)
    monkeypatch.setattr(servicos.provedor, "reembolsar", lambda *a: pytest.fail("provedor não deveria ser chamado"))
    servicos.comandos.executar(reembolsar(envelope["os_id"], envelope["saga_id"]), AGORA)
    assert tipos(bd)[-2:] == ["PagamentoReembolsado"] * 2


def test_reembolso_usa_a_mesma_chave_de_idempotencia(servicos, bd, monkeypatch):
    envelope, pagamento = confirmar(servicos, bd)
    chaves = []
    monkeypatch.setattr(servicos.provedor, "reembolsar", lambda ref, chave: chaves.append((ref, chave)))
    servicos.comandos.executar(reembolsar(envelope["os_id"], envelope["saga_id"]), AGORA)
    assert chaves == [(pagamento["provedor_ref"], f"reembolso:{pagamento['_id']}")]


def test_falha_no_reembolso_publica_reembolso_falhou(servicos, bd, monkeypatch):
    envelope, pagamento = confirmar(servicos, bd)

    def recusar(*_):
        raise FalhaNoProvedor("HTTP 400: saldo insuficiente")
    monkeypatch.setattr(servicos.provedor, "reembolsar", recusar)
    servicos.comandos.executar(reembolsar(envelope["os_id"], envelope["saga_id"]), AGORA)
    assert bd["pagamentos"].find_one({})["estado"] == "REEMBOLSO_FALHOU"
    assert publicadas(bd)[-1]["payload"] == {"pagamento_id": pagamento["_id"], "detalhe": "HTTP 400: saldo insuficiente"}
    assert tipos(bd)[-1] == "ReembolsoFalhou"


def test_reembolsar_sem_pagamento_confirma_com_zero(servicos, bd):
    servicos.comandos.executar(reembolsar(uuid.uuid4(), uuid.uuid4()), AGORA)
    assert publicadas(bd)[-1]["payload"] == {"pagamento_id": "", "valor": "0.00"}


def test_reembolsar_pagamento_que_nao_foi_confirmado_devolve_o_que_houver(servicos, bd):
    envelope, pagamento = pagamento_pendente(servicos, bd)
    servicos.comandos.executar(reembolsar(envelope["os_id"], envelope["saga_id"]), AGORA)
    assert publicadas(bd)[-1]["payload"] == {"pagamento_id": pagamento["_id"], "valor": "220.00"}
    assert bd["pagamentos"].find_one({})["estado"] == "PENDENTE"


def test_conflito_de_estado_desfaz_a_transacao_inteira(servicos, bd, monkeypatch):
    envelope, pagamento = pagamento_pendente(servicos, bd)
    antes = len(publicadas(bd))
    cancelar = servicos.provedor.cancelar

    def cancelar_com_corrida(ref):
        bd["pagamentos"].update_one({"_id": pagamento["_id"]}, {"$set": {"estado": EstadoPagamento.CANCELADO.value}})
        return cancelar(ref)
    monkeypatch.setattr(servicos.provedor, "cancelar", cancelar_com_corrida)
    mensagem = cancelar_orcamento(envelope["os_id"], envelope["saga_id"])
    with pytest.raises(ConflictException):
        servicos.comandos.executar(mensagem, AGORA)
    assert len(publicadas(bd)) == antes and bd["mensagens_processadas"].find_one({"_id": mensagem["message_id"]}) is None
    assert bd["orcamentos"].find_one({})["estado"] == "APROVADO"
