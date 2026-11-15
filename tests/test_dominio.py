import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

from app.domain.dinheiro import dinheiro
from app.domain.entities.orcamento import EstadoOrcamento, Orcamento
from app.domain.entities.pagamento import EstadoPagamento, Pagamento
from app.domain.exceptions import BusinessRuleException
from tests.apoio import AGORA


def orcamento(estado=EstadoOrcamento.GERADO) -> Orcamento:
    return Orcamento(id=str(uuid.uuid4()), os_id=str(uuid.uuid4()), saga_id=str(uuid.uuid4()), cliente={"id": "c1", "nome": "Ana"},
                     itens=[], valor_total=Decimal("220.00"), expira_em=AGORA + timedelta(hours=1), estado=estado)


def pagamento(estado=EstadoPagamento.PENDENTE) -> Pagamento:
    return Pagamento(id="p1", orcamento_id="o1", os_id="os1", saga_id="s1", provedor="SIMULADO", valor=Decimal("220.00"),
                     provedor_ref="ref", qr_data="qr", expira_em=AGORA + timedelta(minutes=15), estado=estado)


def test_orcamento_aprovado_guarda_quem_aprovou():
    o = orcamento()
    o.aprovar("CLIENTE", AGORA)
    assert (o.estado, o.aprovado_por) == (EstadoOrcamento.APROVADO, "CLIENTE")


def test_orcamento_vencido_nao_pode_ser_aprovado():
    o = orcamento()
    with pytest.raises(BusinessRuleException, match="Orçamento expirado"):
        o.aprovar("CLIENTE", AGORA + timedelta(hours=1))
    assert o.estado == EstadoOrcamento.GERADO


@pytest.mark.parametrize("estado", [e for e in EstadoOrcamento if e != EstadoOrcamento.GERADO])
def test_so_orcamento_gerado_e_decidido(estado):
    for acao in (lambda o: o.aprovar("CLIENTE", AGORA), lambda o: o.rejeitar(), lambda o: o.expirar(AGORA + timedelta(days=1))):
        with pytest.raises(BusinessRuleException):
            acao(orcamento(estado))


def test_orcamento_rejeitado():
    o = orcamento()
    o.rejeitar()
    assert o.estado == EstadoOrcamento.REJEITADO


def test_orcamento_expira_so_depois_do_prazo():
    o = orcamento()
    with pytest.raises(BusinessRuleException, match="no prazo"):
        o.expirar(AGORA)
    o.expirar(AGORA + timedelta(hours=1))
    assert o.estado == EstadoOrcamento.EXPIRADO


@pytest.mark.parametrize("estado, esperado", [
    (EstadoOrcamento.GERADO, EstadoOrcamento.CANCELADO),
    (EstadoOrcamento.APROVADO, EstadoOrcamento.CANCELADO),
    (EstadoOrcamento.CANCELADO, EstadoOrcamento.CANCELADO),
    (EstadoOrcamento.REJEITADO, EstadoOrcamento.REJEITADO),
    (EstadoOrcamento.EXPIRADO, EstadoOrcamento.EXPIRADO),
])
def test_cancelar_orcamento(estado, esperado):
    o = orcamento(estado)
    o.cancelar()
    assert o.estado == esperado


def test_pagamento_confirmado_guarda_a_data():
    p = pagamento()
    p.confirmar(AGORA)
    assert (p.estado, p.pago_em) == (EstadoPagamento.CONFIRMADO, AGORA)


@pytest.mark.parametrize("acao, esperado", [
    (lambda p: p.recusar(), EstadoPagamento.RECUSADO),
    (lambda p: p.expirar(), EstadoPagamento.EXPIRADO),
    (lambda p: p.cancelar(), EstadoPagamento.CANCELADO),
])
def test_pagamento_pendente_pode_ser_encerrado(acao, esperado):
    p = pagamento()
    acao(p)
    assert p.estado == esperado


@pytest.mark.parametrize("acao, esperado", [
    (lambda p: p.reembolsar(), EstadoPagamento.REEMBOLSADO),
    (lambda p: p.falhar_reembolso(), EstadoPagamento.REEMBOLSO_FALHOU),
])
def test_pagamento_confirmado_pode_ser_reembolsado(acao, esperado):
    p = pagamento(EstadoPagamento.CONFIRMADO)
    acao(p)
    assert p.estado == esperado


@pytest.mark.parametrize("estado", [e for e in EstadoPagamento if e != EstadoPagamento.PENDENTE])
def test_so_pagamento_pendente_e_encerrado(estado):
    for acao in (lambda p: p.confirmar(AGORA), lambda p: p.recusar(), lambda p: p.expirar(), lambda p: p.cancelar()):
        with pytest.raises(BusinessRuleException):
            acao(pagamento(estado))


@pytest.mark.parametrize("estado", [e for e in EstadoPagamento if e != EstadoPagamento.CONFIRMADO])
def test_so_pagamento_confirmado_e_reembolsado(estado):
    for acao in (lambda p: p.reembolsar(), lambda p: p.falhar_reembolso()):
        with pytest.raises(BusinessRuleException):
            acao(pagamento(estado))


def test_id_do_pagamento_e_estavel_por_orcamento():
    assert Pagamento.identificar("o1") == Pagamento.identificar("o1") != Pagamento.identificar("o2")
    assert uuid.UUID(Pagamento.identificar("o1")).version == 5


def test_dinheiro_sempre_com_duas_casas():
    assert [dinheiro(Decimal(v)) for v in ("150", "35.5", "0", "1.005")] == ["150.00", "35.50", "0.00", "1.01"]
