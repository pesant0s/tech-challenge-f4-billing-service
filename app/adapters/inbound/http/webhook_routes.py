import hashlib
import hmac
import logging

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query

from app.adapters.inbound.http.dependencias import obter_servicos
from app.infrastructure.config import settings

logger = logging.getLogger("oficina.webhook")

router = APIRouter(prefix="/billing", tags=["💳 Orçamento e Pagamento"])


@router.post("/webhooks/mercadopago", summary="Notificação do Mercado Pago",
    description="Sem JWT: a autenticidade vem da assinatura `x-signature`. A situação do pedido é sempre consultada no provedor.")
def webhook_mercadopago(corpo: dict = Body(default_factory=dict), data_id: str | None = Query(None, alias="data.id"),
                        x_signature: str = Header(""), x_request_id: str | None = Header(None), servicos=Depends(obter_servicos)):
    partes = dict(p.strip().split("=", 1) for p in x_signature.split(",") if "=" in p)
    manifesto = "".join((
        f"id:{data_id.lower() if data_id.isalnum() else data_id};" if data_id else "",
        f"request-id:{x_request_id};" if x_request_id else "",
        f"ts:{partes['ts']};" if "ts" in partes else "",
    ))
    segredo = settings.MP_WEBHOOK_SECRET
    esperada = hmac.new(segredo.encode(), manifesto.encode(), hashlib.sha256).hexdigest() if segredo else None
    if esperada is None or not hmac.compare_digest(esperada, partes.get("v1", "")):
        logger.warning("Notificação com assinatura inválida", extra={"evento": "webhook_assinatura_invalida"})
        raise HTTPException(401, "Assinatura inválida")
    if corpo.get("type") != "order":
        return {"processado": False}
    return {"processado": servicos.notificacao_mercadopago.executar(data_id or corpo["data"]["id"])}
