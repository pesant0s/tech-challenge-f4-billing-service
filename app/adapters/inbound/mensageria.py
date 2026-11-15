"""Consumo dos comandos da Saga: ack só depois do commit (ADR-021)."""
import json
import logging
import uuid

from app.application.use_cases.comandos_saga import COMANDOS
from app.infrastructure.logging_config import definir_correlation_id
from app.infrastructure.rastreamento import consumo_rastreado

logger = logging.getLogger("oficina.mensageria")


def tratar_mensagem(canal, metodo, propriedades, corpo, comandos) -> None:
    """Mensagem ilegível ou de comando desconhecido vai direto para a DLQ; erro inesperado volta para a fila."""
    try:
        envelope = json.loads(corpo)
        tipo, saga_id = envelope["tipo"], str(uuid.UUID(envelope["saga_id"]))
        uuid.UUID(envelope["message_id"])
        uuid.UUID(envelope["os_id"])
        if tipo not in COMANDOS:
            raise ValueError(tipo)
    except (ValueError, KeyError, TypeError, AttributeError):
        logger.error("Mensagem ilegível descartada para a DLQ", extra={"evento": "mensagem_invalida"})
        canal.basic_nack(metodo.delivery_tag, requeue=False)
        return
    definir_correlation_id(saga_id)
    with consumo_rastreado(tipo, propriedades.headers):
        try:
            comandos.executar(envelope)
        except Exception:
            logger.exception("Falha ao processar %s; a mensagem volta para a fila", tipo, extra={"evento": "falha_no_consumo", "tipo": tipo})
            canal.basic_nack(metodo.delivery_tag, requeue=True)
            return
    canal.basic_ack(metodo.delivery_tag)
