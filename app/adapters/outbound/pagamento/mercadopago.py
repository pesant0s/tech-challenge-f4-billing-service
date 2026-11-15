"""Orders API do Mercado Pago com QR Code dinâmico (ADR-023)."""
import math
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from app.domain.dinheiro import dinheiro
from app.domain.ports.provedor_pagamento import Cobranca, FalhaNoProvedor, SituacaoPagamento

# A documentação diverge nos caminhos de cancelamento e reembolso: ajuste aqui depois de validar no sandbox.
CAMINHO_CRIAR = "/v1/orders"
CAMINHO_PEDIDO = "/v1/orders/{id}"
CAMINHO_CANCELAR = "/v1/orders/{id}/cancel"
CAMINHO_REEMBOLSAR = "/v1/orders/{id}/refund"

SITUACOES = {
    "processed": SituacaoPagamento.PAGO,
    "canceled": SituacaoPagamento.CANCELADO,
    "expired": SituacaoPagamento.EXPIRADO,
    "refunded": SituacaoPagamento.REEMBOLSADO,
    "failed": SituacaoPagamento.RECUSADO,
}


def _situacao(pedido: dict) -> SituacaoPagamento:
    if pedido.get("status") in SITUACOES:
        return SITUACOES[pedido["status"]]
    pagamentos = (pedido.get("transactions") or {}).get("payments") or []
    if any(p.get("status") in ("rejected", "failed") for p in pagamentos):
        return SituacaoPagamento.RECUSADO
    return SituacaoPagamento.PENDENTE


class ProvedorMercadoPago:
    nome = "MERCADO_PAGO"

    def __init__(self, url_base: str, access_token: str, external_pos_id: str):
        self._pos_id = external_pos_id
        self._http = httpx.Client(base_url=url_base, timeout=10, headers={"Authorization": f"Bearer {access_token}"})

    def criar_cobranca(self, pagamento_id: str, os_id: str, valor: Decimal, descricao: str, expira_em: datetime) -> Cobranca:
        minutos = max(1, math.ceil((expira_em - datetime.now(timezone.utc)).total_seconds() / 60))
        resposta = self._http.post(CAMINHO_CRIAR, headers={"X-Idempotency-Key": pagamento_id}, json={
            "type": "qr",
            "external_reference": os_id,
            "description": descricao[:150],
            "total_amount": dinheiro(valor),
            "expiration_time": f"PT{minutos}M",
            "config": {"qr": {"external_pos_id": self._pos_id, "mode": "dynamic"}},
            "transactions": {"payments": [{"amount": dinheiro(valor)}]},
        })
        resposta.raise_for_status()
        pedido = resposta.json()
        qr_data = (pedido.get("type_response") or {}).get("qr_data") or pedido.get("qr_data")
        return Cobranca(provedor_ref=pedido["id"], qr_data=qr_data, expira_em=expira_em)

    def consultar(self, provedor_ref: str) -> SituacaoPagamento:
        resposta = self._http.get(CAMINHO_PEDIDO.format(id=provedor_ref))
        resposta.raise_for_status()
        return _situacao(resposta.json())

    def cancelar(self, provedor_ref: str) -> SituacaoPagamento:
        resposta = self._http.post(CAMINHO_CANCELAR.format(id=provedor_ref))
        if resposta.status_code == 409:
            return self.consultar(provedor_ref)
        resposta.raise_for_status()
        return _situacao(resposta.json())

    def reembolsar(self, provedor_ref: str, chave_idempotencia: str) -> None:
        resposta = self._http.post(CAMINHO_REEMBOLSAR.format(id=provedor_ref), headers={"X-Idempotency-Key": chave_idempotencia})
        if resposta.status_code >= 400:
            raise FalhaNoProvedor(f"HTTP {resposta.status_code}: {resposta.text[:200]}")
