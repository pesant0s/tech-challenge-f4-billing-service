"""Conciliação periódica: fecha o que o provedor já resolveu e o que passou do prazo (ADR-023)."""
import logging
from datetime import datetime, timezone

from app.application.mensagens import pagamento_confirmado, pagamento_recusado, publicar_evento
from app.domain.entities.orcamento import EstadoOrcamento
from app.domain.entities.pagamento import EstadoPagamento, Pagamento
from app.domain.exceptions import ConflictException
from app.domain.ports.provedor_pagamento import ProvedorPagamento, SituacaoPagamento

logger = logging.getLogger("oficina.conciliacao")

LOTE = 50


class ConciliarPagamentosUseCase:
    def __init__(self, banco, provedor: ProvedorPagamento):
        self._banco = banco
        self._provedor = provedor

    def executar(self, agora: datetime | None = None) -> None:
        """Expira os orçamentos vencidos e concilia os pagamentos pendentes, em lotes."""
        agora = agora or datetime.now(timezone.utc)
        ultimo = ""
        while lote := self._banco.leitura().orcamentos.vencidos(agora, ultimo, LOTE):
            for orcamento in lote:
                self._tentar(self._expirar_orcamento, orcamento, agora)
            ultimo = lote[-1].id
        ultimo = ""
        while lote := self._banco.leitura().pagamentos.pendentes(ultimo, LOTE):
            for pagamento in lote:
                self._tentar(self.conciliar, pagamento, agora)
            ultimo = lote[-1].id

    @staticmethod
    def _tentar(passo, entidade, agora) -> None:
        try:
            passo(entidade, agora)
        except Exception:
            logger.exception("Falha ao conciliar %s; fica para a próxima volta", entidade.id, extra={"evento": "conciliacao_falhou"})

    def _expirar_orcamento(self, orcamento, agora: datetime) -> bool:
        try:
            with self._banco.transacao() as repos:
                orcamento.expirar(agora)
                repos.orcamentos.atualizar(orcamento, EstadoOrcamento.GERADO)
                publicar_evento(repos.outbox, "OrcamentoRejeitado", orcamento.saga_id, orcamento.os_id,
                                {"orcamento_id": orcamento.id, "motivo": "ORCAMENTO_EXPIRADO"}, agora)
        except ConflictException:
            return False
        return True

    def conciliar(self, pagamento: Pagamento, agora: datetime) -> bool:
        """Consulta o provedor e aplica o resultado; False quando nada mudou ou outra réplica chegou antes."""
        if pagamento.estado != EstadoPagamento.PENDENTE:
            return False
        situacao = self._provedor.consultar(pagamento.provedor_ref)
        if situacao == SituacaoPagamento.EXPIRADO or (situacao == SituacaoPagamento.PENDENTE and agora >= pagamento.expira_em):
            # Por garantia: o QR pode ter sido pago no instante em que expirou.
            situacao = SituacaoPagamento.PAGO if self._provedor.cancelar(pagamento.provedor_ref) == SituacaoPagamento.PAGO \
                else SituacaoPagamento.EXPIRADO
        if situacao in (SituacaoPagamento.PENDENTE, SituacaoPagamento.REEMBOLSADO):
            return False
        try:
            with self._banco.transacao() as repos:
                evento = self._aplicar(pagamento, situacao, agora)
                repos.pagamentos.atualizar(pagamento, EstadoPagamento.PENDENTE)
                if evento:
                    tipo, payload = evento
                    publicar_evento(repos.outbox, tipo, pagamento.saga_id, pagamento.os_id, payload, agora)
        except ConflictException:
            return False
        logger.info("Pagamento %s → %s", pagamento.id, pagamento.estado.value, extra={
            "evento": "pagamento_conciliado", "os_id": pagamento.os_id, "pagamento_id": pagamento.id, "estado": pagamento.estado.value,
        })
        return True

    @staticmethod
    def _aplicar(pagamento: Pagamento, situacao: SituacaoPagamento, agora: datetime) -> tuple[str, dict] | None:
        """Muda o pagamento e devolve o evento da Saga, se houver."""
        if situacao == SituacaoPagamento.PAGO:
            pagamento.confirmar(agora)
            return "PagamentoConfirmado", pagamento_confirmado(pagamento)
        if situacao == SituacaoPagamento.RECUSADO:
            pagamento.recusar()
            return "PagamentoRecusado", pagamento_recusado(pagamento.id, "PAGAMENTO_RECUSADO")
        if situacao == SituacaoPagamento.EXPIRADO:
            pagamento.expirar()
            return "PagamentoRecusado", pagamento_recusado(pagamento.id, "PAGAMENTO_EXPIRADO")
        pagamento.cancelar()
        return None
