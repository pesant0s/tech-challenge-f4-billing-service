from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from app.domain.entities.orcamento import Orcamento
from app.domain.entities.pagamento import Pagamento


class PagamentoResponse(BaseModel):
    id: str
    provedor: str
    estado: str
    valor: Decimal
    qr_data: str
    expira_em: datetime
    pago_em: datetime | None


class OrcamentoResponse(BaseModel):
    id: str
    os_id: str
    estado: str
    cliente: dict
    itens: list[dict]
    valor_total: Decimal
    expira_em: datetime
    aprovado_por: str | None
    criado_em: datetime
    pagamento: PagamentoResponse | None = None

    @classmethod
    def montar(cls, orcamento: Orcamento, pagamento: Pagamento | None = None) -> "OrcamentoResponse":
        return cls(
            **{**orcamento.__dict__, "estado": orcamento.estado.value},
            pagamento=pagamento and PagamentoResponse(**{**pagamento.__dict__, "estado": pagamento.estado.value}),
        )
