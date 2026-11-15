from datetime import datetime, timezone

from app.application.mensagens import publicar_evento
from app.domain.entities.orcamento import EstadoOrcamento, Orcamento
from app.domain.entities.pagamento import Pagamento
from app.domain.exceptions import NotFoundException


class DecidirOrcamentoUseCase:
    """Consulta e decisão do orçamento pela API; `cliente_id` restringe ao titular (None para a oficina)."""

    def __init__(self, banco):
        self._banco = banco

    @staticmethod
    def _titular(orcamento: Orcamento | None, cliente_id: str | None) -> Orcamento:
        if orcamento is None or (cliente_id is not None and orcamento.cliente["id"] != cliente_id):
            raise NotFoundException("Orçamento não encontrado")
        return orcamento

    def obter(self, os_id: str, cliente_id: str | None) -> tuple[Orcamento, Pagamento | None]:
        leitura = self._banco.leitura()
        orcamento = self._titular(leitura.orcamentos.buscar_por_os(os_id), cliente_id)
        return orcamento, leitura.pagamentos.buscar_por_orcamento(orcamento.id)

    def aprovar(self, os_id: str, por: str, cliente_id: str | None, agora: datetime | None = None) -> Orcamento:
        agora = agora or datetime.now(timezone.utc)
        with self._banco.transacao() as repos:
            orcamento = self._titular(repos.orcamentos.buscar_por_os(os_id), cliente_id)
            orcamento.aprovar(por, agora)
            repos.orcamentos.atualizar(orcamento, EstadoOrcamento.GERADO)
            publicar_evento(repos.outbox, "OrcamentoAprovado", orcamento.saga_id, os_id,
                            {"orcamento_id": orcamento.id, "aprovado_por": por}, agora)
        return orcamento

    def rejeitar(self, os_id: str, cliente_id: str | None, agora: datetime | None = None) -> Orcamento:
        agora = agora or datetime.now(timezone.utc)
        with self._banco.transacao() as repos:
            orcamento = self._titular(repos.orcamentos.buscar_por_os(os_id), cliente_id)
            orcamento.rejeitar()
            repos.orcamentos.atualizar(orcamento, EstadoOrcamento.GERADO)
            publicar_evento(repos.outbox, "OrcamentoRejeitado", orcamento.saga_id, os_id,
                            {"orcamento_id": orcamento.id, "motivo": "ORCAMENTO_REJEITADO"}, agora)
        return orcamento
