import logging

from app.domain.dinheiro import dinheiro
from app.domain.entities.orcamento import Orcamento

logger = logging.getLogger("oficina.notificacoes")


class NotificacaoSimulada:
    """Registra o envio em log, sem provedor de e-mail ou SMS (ADR-016)."""

    def enviar(self, orcamento: Orcamento) -> None:
        destinatario = orcamento.cliente.get("email") or orcamento.cliente.get("telefone")
        logger.info("Orçamento da OS %s enviado a %s (R$ %s)", orcamento.os_id, destinatario, dinheiro(orcamento.valor_total), extra={
            "evento": "orcamento_enviado", "os_id": orcamento.os_id,
            "destinatario": destinatario, "valor_total": dinheiro(orcamento.valor_total),
        })
