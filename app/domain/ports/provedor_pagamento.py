import enum
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol


class SituacaoPagamento(str, enum.Enum):
    PENDENTE = "PENDENTE"
    PAGO = "PAGO"
    RECUSADO = "RECUSADO"
    EXPIRADO = "EXPIRADO"
    CANCELADO = "CANCELADO"
    REEMBOLSADO = "REEMBOLSADO"


@dataclass(frozen=True)
class Cobranca:
    provedor_ref: str
    qr_data: str
    expira_em: datetime


class FalhaNoProvedor(Exception):
    def __init__(self, detalhe: str):
        super().__init__(detalhe)
        self.detalhe = detalhe


class ProvedorPagamento(Protocol):
    nome: str

    def criar_cobranca(self, pagamento_id: str, os_id: str, valor: Decimal, descricao: str, expira_em: datetime) -> Cobranca:
        """Idempotente pelo `pagamento_id`: repetir a chamada devolve a mesma cobrança."""

    def consultar(self, provedor_ref: str) -> SituacaoPagamento: ...

    def cancelar(self, provedor_ref: str) -> SituacaoPagamento:
        """Devolve a situação final; um pedido já pago continua PAGO."""

    def reembolsar(self, provedor_ref: str, chave_idempotencia: str) -> None:
        """Levanta `FalhaNoProvedor` quando o provedor recusa o reembolso."""
