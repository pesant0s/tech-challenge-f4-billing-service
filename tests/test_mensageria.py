"""Consumidor, relay e topologia com um canal falso; a prova contra um RabbitMQ real só roda se houver um."""
import json
import os as sistema
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pika.exceptions import UnroutableError

from app.adapters.inbound.mensageria import tratar_mensagem
from app.adapters.outbound.mensageria import FILA_COMANDOS, declarar_topologia, publicar_pendentes
from app.application.mensagens import publicar_evento
from app.domain.ports.provedor_pagamento import SituacaoPagamento
from app.infrastructure.rastreamento import cabecalhos_de_rastreamento, consumo_rastreado
from tests.apoio import gerar_orcamento, pagamento_pendente, publicadas


class CanalFalso:
    def __init__(self, recusar_rotas=()):
        self.publicadas, self.acks, self.nacks, self.chamadas = [], [], [], []
        self._recusar = recusar_rotas

    def basic_publish(self, exchange, rota, corpo, propriedades, mandatory):
        if rota in self._recusar:
            raise UnroutableError([])
        self.publicadas.append((exchange, rota, json.loads(corpo), propriedades))

    def basic_ack(self, tag):
        self.acks.append(tag)

    def basic_nack(self, tag, requeue):
        self.nacks.append((tag, requeue))

    def __getattr__(self, nome):
        return lambda *args, **kwargs: self.chamadas.append((nome, args, kwargs))


@pytest.fixture
def pedido():
    return gerar_orcamento(uuid.uuid4(), uuid.uuid4())


def entregar(canal, corpo, servicos, headers=None):
    tratar_mensagem(canal, SimpleNamespace(delivery_tag=7), SimpleNamespace(headers=headers), corpo, servicos.comandos)


def test_comando_valido_e_confirmado_depois_do_processamento(pedido, servicos, bd):
    canal = CanalFalso()
    entregar(canal, json.dumps(pedido), servicos, headers={"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"})
    assert canal.acks == [7] and canal.nacks == []
    assert [m["tipo"] for m in publicadas(bd)] == ["OrcamentoGerado"]


def test_comando_entregue_duas_vezes_responde_uma_so(pedido, servicos, bd):
    canal = CanalFalso()
    entregar(canal, json.dumps(pedido), servicos)
    entregar(canal, json.dumps(pedido), servicos)
    assert canal.acks == [7, 7] and len(publicadas(bd)) == 1


@pytest.mark.parametrize("corpo", [
    b"{nao e json",
    json.dumps({"tipo": "GerarOrcamento"}),
    json.dumps({"tipo": "ReservarPecas", "message_id": str(uuid.uuid4()), "saga_id": str(uuid.uuid4()), "os_id": str(uuid.uuid4())}),
    json.dumps({"tipo": "Cobrar", "message_id": "x", "saga_id": str(uuid.uuid4()), "os_id": str(uuid.uuid4())}),
], ids=["não é json", "sem campos", "comando de outro serviço", "message_id inválido"])
def test_mensagem_ilegivel_vai_direto_para_a_dlq(corpo, servicos):
    canal = CanalFalso()
    entregar(canal, corpo, servicos)
    assert canal.nacks == [(7, False)] and canal.acks == []


def test_falha_inesperada_devolve_a_mensagem_para_a_fila(pedido, servicos, bd):
    pedido["payload"] = {"itens": "quebrado"}
    canal = CanalFalso()
    entregar(canal, json.dumps(pedido), servicos)
    assert canal.nacks == [(7, True)] and canal.acks == []
    assert publicadas(bd) == [] and bd["mensagens_processadas"].count_documents({}) == 0


def test_relay_publica_o_outbox_com_as_propriedades_do_contrato(pedido, servicos, bd):
    entregar(CanalFalso(), json.dumps(pedido), servicos)
    canal = CanalFalso()
    assert publicar_pendentes(canal, servicos.banco) == 1
    exchange, rota, corpo, propriedades = canal.publicadas[0]
    assert (exchange, rota, corpo["tipo"]) == ("oficina.eventos", "billing.orcamento-gerado", "OrcamentoGerado")
    assert propriedades.message_id == corpo["message_id"] and propriedades.type == "OrcamentoGerado"
    assert propriedades.correlation_id == pedido["saga_id"] and propriedades.delivery_mode == 2
    assert bd["outbox"].find_one()["publicado_em"] is not None
    assert publicar_pendentes(canal, servicos.banco) == 0


def test_relay_para_quando_o_broker_recusa_e_tenta_de_novo_depois(pedido, servicos, bd):
    entregar(CanalFalso(), json.dumps(pedido), servicos)
    assert publicar_pendentes(CanalFalso(recusar_rotas={"billing.orcamento-gerado"}), servicos.banco) == 0
    mensagem = bd["outbox"].find_one()
    assert mensagem["publicado_em"] is None and mensagem["reservada_ate"] is None
    assert publicar_pendentes(CanalFalso(), servicos.banco) == 1


def test_relay_respeita_o_limite_e_a_ordem(servicos, bd):
    for _ in range(3):
        servicos.comandos.executar(gerar_orcamento(uuid.uuid4(), uuid.uuid4()), datetime.now(timezone.utc))
    canal = CanalFalso()
    assert publicar_pendentes(canal, servicos.banco, limite=2) == 2
    assert publicar_pendentes(canal, servicos.banco, limite=2) == 1
    ordem = [c["message_id"] for c in publicadas(bd)]
    assert [corpo["message_id"] for _, _, corpo, _ in canal.publicadas] == ordem


def test_duas_replicas_nao_reivindicam_a_mesma_mensagem(servicos, bd):
    for _ in range(2):
        servicos.comandos.executar(gerar_orcamento(uuid.uuid4(), uuid.uuid4()), datetime.now(timezone.utc))
    outbox = servicos.banco.leitura().outbox
    agora = datetime.now(timezone.utc)
    primeira, segunda = outbox.reivindicar(agora), outbox.reivindicar(agora)
    assert primeira["_id"] != segunda["_id"] and outbox.reivindicar(agora) is None


def test_reserva_vencida_deixa_a_mensagem_disponivel_de_novo(servicos, bd):
    servicos.comandos.executar(gerar_orcamento(uuid.uuid4(), uuid.uuid4()), datetime.now(timezone.utc))
    outbox = servicos.banco.leitura().outbox
    agora = datetime.now(timezone.utc)
    reivindicada = outbox.reivindicar(agora)
    assert outbox.reivindicar(agora + timedelta(seconds=29)) is None
    assert outbox.reivindicar(agora + timedelta(seconds=31))["_id"] == reivindicada["_id"]


@pytest.mark.parametrize("tipo, rota", [
    ("OrcamentoGerado", "billing.orcamento-gerado"),
    ("OrcamentoAprovado", "billing.orcamento-aprovado"),
    ("OrcamentoRejeitado", "billing.orcamento-rejeitado"),
    ("OrcamentoCancelado", "billing.orcamento-cancelado"),
    ("CobrancaCriada", "billing.cobranca-criada"),
    ("PagamentoConfirmado", "billing.pagamento-confirmado"),
    ("PagamentoRecusado", "billing.pagamento-recusado"),
    ("PagamentoReembolsado", "billing.pagamento-reembolsado"),
    ("ReembolsoFalhou", "billing.reembolso-falhou"),
])
def test_chave_de_roteamento_segue_o_contrato(servicos, bd, tipo, rota):
    publicar_evento(servicos.banco.leitura().outbox, tipo, uuid.uuid4(), uuid.uuid4(), {}, datetime.now(timezone.utc))
    linha = bd["outbox"].find_one()
    assert (linha["exchange"], linha["rota"]) == ("oficina.eventos", rota)
    assert linha["corpo"]["ocorrido_em"].endswith("Z") and linha["corpo"]["origem"] == "billing-service"


def test_topologia_declara_fila_quorum_com_limite_e_dlq():
    canal = CanalFalso()
    declarar_topologia(canal)
    filas = {args[0]: kwargs["arguments"] for nome, args, kwargs in canal.chamadas if nome == "queue_declare"}
    assert filas[FILA_COMANDOS] == {"x-queue-type": "quorum", "x-delivery-limit": 5,
                                    "x-dead-letter-exchange": "oficina.dlx", "x-dead-letter-routing-key": FILA_COMANDOS}
    ligacoes = {(args[0], args[1], args[2]) for nome, args, _ in canal.chamadas if nome == "queue_bind"}
    assert ligacoes == {(FILA_COMANDOS, "oficina.comandos", "billing.#"), (f"{FILA_COMANDOS}.dlq", "oficina.dlx", FILA_COMANDOS)}


def test_rastreamento_sem_agente_nao_interfere():
    assert cabecalhos_de_rastreamento() == {}
    with consumo_rastreado("GerarOrcamento", None):
        pass


def test_rastreamento_com_agente_propaga_o_contexto(monkeypatch):
    monkeypatch.setattr("newrelic.agent.current_transaction", lambda: object())
    monkeypatch.setattr("newrelic.agent.insert_distributed_trace_headers", lambda c: c.append(("traceparent", "00-abc-def-01")))
    assert cabecalhos_de_rastreamento() == {"traceparent": "00-abc-def-01"}


RABBITMQ_URL = sistema.environ.get("RABBITMQ_URL_TESTE", "amqp://oficina:oficina@localhost:5672/%2F")


def rabbitmq_disponivel():
    try:
        socket.create_connection(("localhost", 5672), timeout=0.5).close()
        return True
    except OSError:
        return False


def aguardar(canal, fila, tipo):
    for _ in range(100):
        metodo, _, corpo = canal.basic_get(fila, auto_ack=True)
        if metodo and json.loads(corpo)["tipo"] == tipo:
            return json.loads(corpo)
        time.sleep(0.1)


@pytest.fixture
def broker():
    import pika
    conexao = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))
    canal = conexao.channel()
    declarar_topologia(canal)
    canal.queue_purge(FILA_COMANDOS)
    fila_teste = canal.queue_declare("", exclusive=True).method.queue
    canal.queue_bind(fila_teste, "oficina.eventos", "billing.#")
    yield canal, fila_teste
    canal.queue_delete(FILA_COMANDOS)
    canal.queue_delete(f"{FILA_COMANDOS}.dlq")
    conexao.close()


@pytest.mark.skipif(not rabbitmq_disponivel(), reason="RabbitMQ local não está no ar")
def test_trabalhador_contra_rabbitmq_real(pedido, servicos, bd, broker):
    """Publica o GerarOrcamento como o OS Service e confere o OrcamentoGerado que o Billing devolve."""
    from app.infrastructure.trabalhador import TrabalhadorMensageria

    canal, fila_teste = broker
    trabalhador = TrabalhadorMensageria(RABBITMQ_URL, servicos, 1)
    trabalhador.start()
    try:
        canal.basic_publish("oficina.comandos", "billing.gerar-orcamento", json.dumps(pedido).encode())
        resposta = aguardar(canal, fila_teste, "OrcamentoGerado")
        assert resposta and (resposta["saga_id"], resposta["os_id"]) == (pedido["saga_id"], pedido["os_id"])
        assert resposta["payload"]["valor_total"] == "220.00" and resposta["origem"] == "billing-service"
    finally:
        trabalhador.parar()


@pytest.mark.skipif(not rabbitmq_disponivel(), reason="RabbitMQ local não está no ar")
def test_trabalhador_concilia_o_pagamento_periodicamente(servicos, bd, broker):
    from app.infrastructure.trabalhador import TrabalhadorMensageria

    canal, fila_teste = broker
    _, pagamento = pagamento_pendente(servicos, bd, datetime.now(timezone.utc))
    servicos.provedor.definir_situacao(pagamento["provedor_ref"], SituacaoPagamento.PAGO)
    trabalhador = TrabalhadorMensageria(RABBITMQ_URL, servicos, 1)
    trabalhador.start()
    try:
        confirmado = aguardar(canal, fila_teste, "PagamentoConfirmado")
        assert confirmado and confirmado["payload"]["pagamento_id"] == pagamento["_id"]
    finally:
        trabalhador.parar()


def test_trabalhador_continua_tentando_com_o_broker_fora_do_ar(servicos, caplog):
    from app.infrastructure.trabalhador import TrabalhadorMensageria

    trabalhador = TrabalhadorMensageria("amqp://oficina:oficina@localhost:1/%2F", servicos, 1)
    trabalhador.start()
    time.sleep(0.5)
    trabalhador.parar()
    assert not trabalhador.is_alive()
    assert any(getattr(r, "evento", "") == "mensageria_reconectando" for r in caplog.records)
