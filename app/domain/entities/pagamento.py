"""Pagamento de um orçamento aprovado, cobrado por QR Code no provedor."""
import enum
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.domain.exceptions import BusinessRuleException


class EstadoPagamento(str, enum.Enum):
    PENDENTE = "PENDENTE"
    CONFIRMADO = "CONFIRMADO"
    RECUSADO = "RECUSADO"
    EXPIRADO = "EXPIRADO"
    CANCELADO = "CANCELADO"
    REEMBOLSADO = "REEMBOLSADO"
    REEMBOLSO_FALHOU = "REEMBOLSO_FALHOU"


@dataclass
class Pagamento:
    id: str
    orcamento_id: str
    os_id: str
    saga_id: str
    provedor: str
    valor: Decimal
    provedor_ref: str
    qr_data: str
    expira_em: datetime
    estado: EstadoPagamento = EstadoPagamento.PENDENTE
    pago_em: datetime | None = None

    @staticmethod
    def identificar(orcamento_id: str) -> str:
        """Id derivado do orçamento: repetir a cobrança reaproveita a chave de idempotência do provedor."""
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"pagamento:{orcamento_id}"))

    def _transitar(self, origem: EstadoPagamento, destino: EstadoPagamento) -> None:
        if self.estado != origem:
            raise BusinessRuleException(f"Pagamento {self.estado.value.lower()} não pode passar a {destino.value.lower()}")
        self.estado = destino

    def confirmar(self, pago_em: datetime) -> None:
        self._transitar(EstadoPagamento.PENDENTE, EstadoPagamento.CONFIRMADO)
        self.pago_em = pago_em

    def recusar(self) -> None:
        self._transitar(EstadoPagamento.PENDENTE, EstadoPagamento.RECUSADO)

    def expirar(self) -> None:
        self._transitar(EstadoPagamento.PENDENTE, EstadoPagamento.EXPIRADO)

    def cancelar(self) -> None:
        self._transitar(EstadoPagamento.PENDENTE, EstadoPagamento.CANCELADO)

    def reembolsar(self) -> None:
        self._transitar(EstadoPagamento.CONFIRMADO, EstadoPagamento.REEMBOLSADO)

    def falhar_reembolso(self) -> None:
        self._transitar(EstadoPagamento.CONFIRMADO, EstadoPagamento.REEMBOLSO_FALHOU)
