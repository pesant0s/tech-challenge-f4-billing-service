"""Comandos da Saga da OS: cada um muda o estado, grava a resposta no outbox e marca o message_id na mesma transação."""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.application.mensagens import formatar_data, pagamento_confirmado, pagamento_recusado, publicar_evento
from app.domain.dinheiro import dinheiro
from app.domain.entities.orcamento import EstadoOrcamento, Orcamento
from app.domain.entities.pagamento import EstadoPagamento, Pagamento
from app.domain.ports.notificacao import NotificacaoOrcamento
from app.domain.ports.provedor_pagamento import FalhaNoProvedor, ProvedorPagamento, SituacaoPagamento

logger = logging.getLogger("oficina.billing")

COMANDOS = frozenset(("GerarOrcamento", "Cobrar", "CancelarOrcamento", "Reembolsar"))


def _dados_do_orcamento(orcamento: Orcamento) -> dict:
    return {"orcamento_id": orcamento.id, "valor_total": dinheiro(orcamento.valor_total), "expira_em": formatar_data(orcamento.expira_em)}


def _dados_da_cobranca(pagamento: Pagamento) -> dict:
    return {"pagamento_id": pagamento.id, "provedor": pagamento.provedor, "valor": dinheiro(pagamento.valor),
            "qr_data": pagamento.qr_data, "expira_em": formatar_data(pagamento.expira_em)}


class ProcessarComandoUseCase:
    """Aplica um comando do OS Service uma única vez por message_id."""

    def __init__(self, banco, provedor: ProvedorPagamento, notificacao: NotificacaoOrcamento, validade_orcamento: timedelta, expiracao_pagamento: timedelta):
        self._banco = banco
        self._provedor = provedor
        self._notificacao = notificacao
        self._validade = validade_orcamento
        self._expiracao = expiracao_pagamento

    def executar(self, envelope: dict, agora: datetime | None = None) -> bool:
        """False quando a mensagem já tinha sido processada."""
        agora = agora or datetime.now(timezone.utc)
        tratar = {"GerarOrcamento": self._gerar_orcamento, "Cobrar": self._cobrar,
                  "CancelarOrcamento": self._cancelar_orcamento, "Reembolsar": self._reembolsar}[envelope["tipo"]]
        return tratar(envelope, agora)

    def _gravar(self, envelope: dict, agora: datetime, aplicar) -> bool:
        """Abre a transação, descarta a mensagem repetida e publica os eventos que `aplicar` devolve."""
        os_id, saga_id = envelope["os_id"], envelope["saga_id"]
        with self._banco.transacao() as repos:
            if not repos.inbox.registrar(envelope["message_id"], envelope["tipo"], agora):
                logger.info("Mensagem %s repetida, ignorada", envelope["message_id"], extra={"evento": "mensagem_repetida", "tipo": envelope["tipo"]})
                return False
            tipos = []
            for tipo, payload in aplicar(repos):
                publicar_evento(repos.outbox, tipo, saga_id, os_id, payload, agora)
                tipos.append(tipo)
        logger.info("%s · %s → %s", os_id, envelope["tipo"], ", ".join(tipos), extra={
            "evento": "comando_processado", "saga_id": saga_id, "os_id": os_id, "tipo": envelope["tipo"], "resposta": tipos,
        })
        return True

    def _gerar_orcamento(self, envelope: dict, agora: datetime) -> bool:
        def aplicar(repos):
            orcamento = repos.orcamentos.buscar_por_os(envelope["os_id"])
            if orcamento is None:
                dados = envelope["payload"]
                orcamento = Orcamento(
                    id=str(uuid.uuid4()), os_id=envelope["os_id"], saga_id=envelope["saga_id"], cliente=dados["cliente"],
                    itens=dados["itens"], valor_total=Decimal(dados["valor_total"]), expira_em=agora + self._validade, criado_em=agora,
                )
                repos.orcamentos.adicionar(orcamento)
                self._notificacao.enviar(orcamento)
            return [("OrcamentoGerado", _dados_do_orcamento(orcamento))]
        return self._gravar(envelope, agora, aplicar)

    def _cobrar(self, envelope: dict, agora: datetime) -> bool:
        orcamento_id = envelope["payload"]["orcamento_id"]
        leitura = self._banco.leitura()
        orcamento = leitura.orcamentos.buscar(orcamento_id)
        cobranca = None
        if orcamento and orcamento.estado == EstadoOrcamento.APROVADO and leitura.pagamentos.buscar_por_orcamento(orcamento_id) is None:
            cobranca = self._provedor.criar_cobranca(
                Pagamento.identificar(orcamento_id), orcamento.os_id, orcamento.valor_total,
                f"Ordem de serviço {orcamento.os_id}", agora + self._expiracao,
            )

        def aplicar(repos):
            pagamento = repos.pagamentos.buscar_por_orcamento(orcamento_id)
            if pagamento is None and cobranca is not None:
                pagamento = Pagamento(
                    id=Pagamento.identificar(orcamento_id), orcamento_id=orcamento_id, os_id=orcamento.os_id, saga_id=orcamento.saga_id,
                    provedor=self._provedor.nome, valor=orcamento.valor_total, provedor_ref=cobranca.provedor_ref,
                    qr_data=cobranca.qr_data, expira_em=cobranca.expira_em,
                )
                repos.pagamentos.adicionar(pagamento)
            if pagamento is None:
                return [("PagamentoRecusado", pagamento_recusado("", "PAGAMENTO_RECUSADO"))]
            return [("CobrancaCriada", _dados_da_cobranca(pagamento))]
        return self._gravar(envelope, agora, aplicar)

    def _cancelar_orcamento(self, envelope: dict, agora: datetime) -> bool:
        leitura = self._banco.leitura()
        orcamento = leitura.orcamentos.buscar_por_os(envelope["os_id"])
        pagamento = orcamento and leitura.pagamentos.buscar_por_orcamento(orcamento.id)
        pendente = bool(pagamento) and pagamento.estado == EstadoPagamento.PENDENTE
        situacao = self._provedor.cancelar(pagamento.provedor_ref) if pendente else None

        def aplicar(repos):
            eventos = []
            if pendente:
                if situacao == SituacaoPagamento.PAGO:
                    pagamento.confirmar(agora)
                    eventos.append(("PagamentoConfirmado", pagamento_confirmado(pagamento)))
                else:
                    pagamento.cancelar()
                repos.pagamentos.atualizar(pagamento, EstadoPagamento.PENDENTE)
            if orcamento:
                anterior = orcamento.estado
                orcamento.cancelar()
                if orcamento.estado != anterior:
                    repos.orcamentos.atualizar(orcamento, anterior)
            return [*eventos, ("OrcamentoCancelado", {"orcamento_id": orcamento.id if orcamento else ""})]
        return self._gravar(envelope, agora, aplicar)

    def _reembolsar(self, envelope: dict, agora: datetime) -> bool:
        leitura = self._banco.leitura()
        orcamento = leitura.orcamentos.buscar_por_os(envelope["os_id"])
        pagamento = orcamento and leitura.pagamentos.buscar_por_orcamento(orcamento.id)
        falha = None
        if pagamento and pagamento.estado == EstadoPagamento.CONFIRMADO:
            try:
                self._provedor.reembolsar(pagamento.provedor_ref, f"reembolso:{pagamento.id}")
            except FalhaNoProvedor as erro:
                falha = erro.detalhe

        def aplicar(repos):
            if pagamento is None:
                return [("PagamentoReembolsado", {"pagamento_id": "", "valor": "0.00"})]
            if pagamento.estado == EstadoPagamento.CONFIRMADO:
                if falha is not None:
                    pagamento.falhar_reembolso()
                    repos.pagamentos.atualizar(pagamento, EstadoPagamento.CONFIRMADO)
                    return [("ReembolsoFalhou", {"pagamento_id": pagamento.id, "detalhe": falha})]
                pagamento.reembolsar()
                repos.pagamentos.atualizar(pagamento, EstadoPagamento.CONFIRMADO)
            return [("PagamentoReembolsado", {"pagamento_id": pagamento.id, "valor": dinheiro(pagamento.valor)})]
        return self._gravar(envelope, agora, aplicar)
