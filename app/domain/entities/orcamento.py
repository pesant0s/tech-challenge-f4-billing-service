"""Orçamento da OS: nasce GERADO e termina aprovado, rejeitado, expirado ou cancelado."""
import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from app.domain.exceptions import BusinessRuleException


class EstadoOrcamento(str, enum.Enum):
    GERADO = "GERADO"
    APROVADO = "APROVADO"
    REJEITADO = "REJEITADO"
    EXPIRADO = "EXPIRADO"
    CANCELADO = "CANCELADO"


@dataclass
class Orcamento:
    id: str
    os_id: str
    saga_id: str
    cliente: dict
    itens: list[dict]
    valor_total: Decimal
    expira_em: datetime
    estado: EstadoOrcamento = EstadoOrcamento.GERADO
    aprovado_por: str | None = None
    criado_em: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def _exigir_gerado(self, acao: str) -> None:
        if self.estado != EstadoOrcamento.GERADO:
            raise BusinessRuleException(f"Orçamento {self.estado.value.lower()} não pode ser {acao}")

    def aprovar(self, por: str, agora: datetime) -> None:
        self._exigir_gerado("aprovado")
        if agora >= self.expira_em:
            raise BusinessRuleException("Orçamento expirado")
        self.estado = EstadoOrcamento.APROVADO
        self.aprovado_por = por

    def rejeitar(self) -> None:
        self._exigir_gerado("rejeitado")
        self.estado = EstadoOrcamento.REJEITADO

    def expirar(self, agora: datetime) -> None:
        self._exigir_gerado("expirado")
        if agora < self.expira_em:
            raise BusinessRuleException("Orçamento ainda está no prazo")
        self.estado = EstadoOrcamento.EXPIRADO

    def cancelar(self) -> None:
        """Só orçamento gerado ou aprovado muda; os demais já estão encerrados."""
        if self.estado in (EstadoOrcamento.GERADO, EstadoOrcamento.APROVADO):
            self.estado = EstadoOrcamento.CANCELADO
