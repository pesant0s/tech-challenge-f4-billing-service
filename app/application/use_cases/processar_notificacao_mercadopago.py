import logging
from datetime import datetime, timezone

logger = logging.getLogger("oficina.conciliacao")


class ProcessarNotificacaoMercadoPagoUseCase:
    """O webhook só avisa que algo mudou: a situação vem sempre de uma consulta ao provedor."""

    def __init__(self, banco, conciliacao):
        self._banco = banco
        self._conciliacao = conciliacao

    def executar(self, pedido_id: str, agora: datetime | None = None) -> bool:
        pagamento = self._banco.leitura().pagamentos.buscar_por_provedor_ref(pedido_id)
        if pagamento is None:
            logger.warning("Notificação do pedido %s sem pagamento correspondente", pedido_id, extra={"evento": "notificacao_sem_pagamento"})
            return False
        return self._conciliacao.conciliar(pagamento, agora or datetime.now(timezone.utc))
