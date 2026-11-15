"""Montagem das dependências do serviço a partir da configuração."""
from datetime import timedelta

from pymongo import MongoClient

from app.adapters.outbound.notifications.notificacao_simulada import NotificacaoSimulada
from app.adapters.outbound.pagamento.mercadopago import ProvedorMercadoPago
from app.adapters.outbound.pagamento.simulado import ProvedorSimulado
from app.adapters.outbound.persistence.banco import Banco
from app.application.use_cases.comandos_saga import ProcessarComandoUseCase
from app.application.use_cases.conciliar_pagamentos import ConciliarPagamentosUseCase
from app.application.use_cases.decidir_orcamento import DecidirOrcamentoUseCase
from app.application.use_cases.processar_notificacao_mercadopago import ProcessarNotificacaoMercadoPagoUseCase


class Servicos:
    def __init__(self, banco: Banco, provedor, validade_orcamento: timedelta, expiracao_pagamento: timedelta):
        self.banco = banco
        self.provedor = provedor
        self.comandos = ProcessarComandoUseCase(banco, provedor, NotificacaoSimulada(), validade_orcamento, expiracao_pagamento)
        self.decisao = DecidirOrcamentoUseCase(banco)
        self.conciliacao = ConciliarPagamentosUseCase(banco, provedor)
        self.notificacao_mercadopago = ProcessarNotificacaoMercadoPagoUseCase(banco, self.conciliacao)


def montar_servicos(config) -> Servicos:
    """Falha na subida, com mensagem clara, quando o Mercado Pago está escolhido sem as credenciais."""
    mercadopago = config.PAGAMENTO_PROVEDOR.lower() == "mercadopago"
    if mercadopago and not (config.MP_ACCESS_TOKEN and config.MP_EXTERNAL_POS_ID):
        raise RuntimeError("PAGAMENTO_PROVEDOR=mercadopago exige MP_ACCESS_TOKEN e MP_EXTERNAL_POS_ID")
    cliente = MongoClient(config.MONGO_URL, tz_aware=True)
    banco_mongo = cliente[config.MONGO_DB]
    provedor = (ProvedorMercadoPago(config.MP_API_URL, config.MP_ACCESS_TOKEN, config.MP_EXTERNAL_POS_ID)
                if mercadopago else ProvedorSimulado(banco_mongo))
    banco = Banco(cliente, banco_mongo)
    banco.preparar_indices()
    return Servicos(banco, provedor, timedelta(minutes=config.ORCAMENTO_VALIDADE_MINUTOS), timedelta(minutes=config.PAGAMENTO_EXPIRACAO_MINUTOS))
