from datetime import datetime
from decimal import Decimal

from pymongo.database import Database

from app.domain.exceptions import NotFoundException
from app.domain.ports.provedor_pagamento import Cobranca, SituacaoPagamento


class ProvedorSimulado:
    """Provedor fictício (ADR-023): a situação fica em `simulador_pagamentos` e o `provedor_ref` é o id do pagamento."""

    nome = "SIMULADO"

    def __init__(self, banco: Database):
        self._colecao = banco["simulador_pagamentos"]

    def criar_cobranca(self, pagamento_id: str, os_id: str, valor: Decimal, descricao: str, expira_em: datetime) -> Cobranca:
        self._colecao.update_one({"_id": pagamento_id}, {"$setOnInsert": {"situacao": SituacaoPagamento.PENDENTE.value}}, upsert=True)
        return Cobranca(provedor_ref=pagamento_id, qr_data=f"00020101SIMULADO{pagamento_id}", expira_em=expira_em)

    def consultar(self, provedor_ref: str) -> SituacaoPagamento:
        return SituacaoPagamento(self._colecao.find_one({"_id": provedor_ref})["situacao"])

    def cancelar(self, provedor_ref: str) -> SituacaoPagamento:
        self._colecao.update_one({"_id": provedor_ref, "situacao": SituacaoPagamento.PENDENTE.value},
                                 {"$set": {"situacao": SituacaoPagamento.CANCELADO.value}})
        return self.consultar(provedor_ref)

    def reembolsar(self, provedor_ref: str, chave_idempotencia: str) -> None:
        self._colecao.update_one({"_id": provedor_ref}, {"$set": {"situacao": SituacaoPagamento.REEMBOLSADO.value}})

    def definir_situacao(self, provedor_ref: str, situacao: SituacaoPagamento) -> None:
        if self._colecao.update_one({"_id": provedor_ref}, {"$set": {"situacao": situacao.value}}).matched_count == 0:
            raise NotFoundException("Pagamento não encontrado no simulador")
