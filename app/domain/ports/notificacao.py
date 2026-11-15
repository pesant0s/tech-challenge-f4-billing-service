from typing import Protocol

from app.domain.entities.orcamento import Orcamento


class NotificacaoOrcamento(Protocol):
    def enviar(self, orcamento: Orcamento) -> None:
        """Avisa o cliente de que o orçamento está disponível para aprovação."""
