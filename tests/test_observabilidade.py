"""Correlação de requisições, logs estruturados, healthchecks e a montagem do serviço."""
import json
import logging
import uuid
from types import SimpleNamespace

import pytest

from app import main
from app.adapters.outbound.pagamento.mercadopago import ProvedorMercadoPago
from app.adapters.outbound.pagamento.simulado import ProvedorSimulado
from app.infrastructure.composicao import montar_servicos
from app.infrastructure.logging_config import ConsoleFormatter, JsonFormatter, definir_correlation_id


def _registro(mensagem: str = "teste", **extras) -> logging.LogRecord:
    registro = logging.LogRecord(
        name="oficina.teste", level=logging.INFO, pathname=__file__,
        lineno=1, msg=mensagem, args=(), exc_info=None,
    )
    for chave, valor in extras.items():
        setattr(registro, chave, valor)
    return registro


def test_log_json_tem_os_campos_obrigatorios():
    definir_correlation_id("abc-123")
    evento = json.loads(JsonFormatter().format(_registro("Orçamento gerado")))
    assert evento["level"] == "INFO"
    assert evento["logger"] == "oficina.teste"
    assert evento["message"] == "Orçamento gerado"
    assert evento["correlation_id"] == "abc-123"
    assert "timestamp" in evento


def test_log_json_carrega_campos_extras():
    definir_correlation_id("abc-123")
    evento = json.loads(JsonFormatter().format(_registro(os_id="os-1", duracao_ms=12.5)))
    assert evento["os_id"] == "os-1"
    assert evento["duracao_ms"] == 12.5


def test_log_json_liga_a_linha_ao_trace_do_new_relic(monkeypatch):
    vinculo = {"trace.id": "t-1", "span.id": "s-1", "entity.guid": "g-1"}
    monkeypatch.setattr("newrelic.agent.get_linking_metadata", lambda: vinculo)
    evento = json.loads(JsonFormatter().format(_registro()))
    assert {k: evento[k] for k in vinculo} == vinculo


def test_log_json_omite_campos_nulos():
    evento = json.loads(JsonFormatter().format(_registro(estado_anterior=None, estado_novo="GERADO")))
    assert "estado_anterior" not in evento
    assert evento["estado_novo"] == "GERADO"


def test_log_json_omite_correlation_id_fora_de_requisicao():
    definir_correlation_id("")
    evento = json.loads(JsonFormatter().format(_registro()))
    assert "correlation_id" not in evento


def test_log_json_serializa_valor_nao_serializavel():
    definir_correlation_id("")
    evento = json.loads(JsonFormatter().format(_registro(objeto=object())))
    assert isinstance(evento["objeto"], str)


def test_log_json_inclui_a_excecao():
    try:
        raise ValueError("falhou")
    except ValueError:
        import sys
        registro = _registro()
        registro.exc_info = sys.exc_info()
    assert "ValueError: falhou" in json.loads(JsonFormatter().format(registro))["exception"]


def test_formato_console_e_legivel():
    definir_correlation_id("abcdef12-3456")
    linha = ConsoleFormatter().format(_registro("mensagem legível"))
    assert "mensagem legível" in linha
    assert "abcdef12" in linha
    assert not linha.startswith("{")


def test_formato_console_inclui_a_excecao():
    try:
        raise ValueError("falhou")
    except ValueError:
        import sys
        registro = _registro()
        registro.exc_info = sys.exc_info()
    assert "ValueError: falhou" in ConsoleFormatter().format(registro)


def test_resposta_devolve_x_request_id(client):
    resposta = client.get("/health")
    assert resposta.status_code == 200
    assert uuid.UUID(resposta.headers["x-request-id"])


def test_correlation_id_recebido_e_preservado(client):
    enviado = "id-vindo-do-api-gateway"
    resposta = client.get("/health", headers={"x-request-id": enviado})
    assert resposta.headers["x-request-id"] == enviado


def test_requisicao_que_falha_e_registrada_e_propagada(client, servicos, atendente_headers, monkeypatch):
    def quebrar(*_):
        raise RuntimeError("falha inesperada")
    monkeypatch.setattr(servicos.decisao, "obter", quebrar)
    from fastapi.testclient import TestClient
    with pytest.raises(RuntimeError):
        TestClient(main.app).get(f"/billing/orcamentos/{uuid.uuid4()}", headers=atendente_headers)


def test_cabecalhos_de_seguranca_presentes(client):
    cabecalhos = client.get("/health").headers
    assert cabecalhos["x-content-type-options"] == "nosniff"
    assert cabecalhos["x-frame-options"] == "DENY"
    assert cabecalhos["referrer-policy"] == "no-referrer"
    assert cabecalhos["server"] == "webserver"


def test_liveness_nao_toca_o_banco(client):
    corpo = client.get("/health").json()
    assert corpo["status"] == "ok"
    assert "banco" not in corpo


def test_readiness_confirma_o_banco(client):
    resposta = client.get("/health/ready")
    assert resposta.status_code == 200
    assert resposta.json()["banco"] == "ok"


def test_readiness_degradada_sem_o_banco(client, servicos, monkeypatch):
    def fora(): raise ConnectionError("mongo fora do ar")
    monkeypatch.setattr(servicos.banco, "ping", fora)
    resposta = client.get("/health/ready")
    assert resposta.status_code == 503 and resposta.json()["banco"] == "inacessivel"


def configuracao(bd, **extra):
    padrao = dict(MONGO_URL="mongodb://localhost:27017/?replicaSet=rs0", MONGO_DB=bd.name, PAGAMENTO_PROVEDOR="simulado",
                  MP_API_URL="https://mp.teste", MP_ACCESS_TOKEN=None, MP_EXTERNAL_POS_ID=None,
                  ORCAMENTO_VALIDADE_MINUTOS=1440, PAGAMENTO_EXPIRACAO_MINUTOS=15)
    return SimpleNamespace(**{**padrao, **extra})


def test_provedor_simulado_e_o_padrao_e_os_indices_sao_criados(bd):
    servicos = montar_servicos(configuracao(bd))
    assert isinstance(servicos.provedor, ProvedorSimulado)
    assert bd["orcamentos"].index_information()["os_id_1"]["unique"] is True
    assert bd["pagamentos"].index_information()["orcamento_id_1"]["unique"] is True
    assert {"provedor_ref_1", "estado_1"} <= set(bd["pagamentos"].index_information())
    assert "publicado_em_1_criado_em_1" in bd["outbox"].index_information()
    assert servicos.banco.leitura().orcamentos.buscar("x") is None


@pytest.mark.parametrize("credenciais", [{}, {"MP_ACCESS_TOKEN": "t"}, {"MP_EXTERNAL_POS_ID": "pos"}])
def test_mercado_pago_sem_credenciais_nao_sobe(bd, credenciais):
    with pytest.raises(RuntimeError, match="MP_ACCESS_TOKEN e MP_EXTERNAL_POS_ID"):
        montar_servicos(configuracao(bd, PAGAMENTO_PROVEDOR="mercadopago", **credenciais))


def test_mercado_pago_com_credenciais(bd):
    config = configuracao(bd, PAGAMENTO_PROVEDOR="MercadoPago", MP_ACCESS_TOKEN="t", MP_EXTERNAL_POS_ID="pos")
    assert isinstance(montar_servicos(config).provedor, ProvedorMercadoPago)


def test_lifespan_monta_os_servicos_e_so_sobe_a_thread_com_rabbitmq(servicos, monkeypatch):
    from fastapi.testclient import TestClient
    iniciados = []

    class TrabalhadorFalso:
        def __init__(self, url, servicos_, segundos):
            iniciados.append((url, servicos_, segundos))

        def start(self):
            iniciados.append("start")

        def parar(self):
            iniciados.append("parar")

    monkeypatch.setattr(main, "montar_servicos", lambda config: servicos)
    monkeypatch.setattr(main, "TrabalhadorMensageria", TrabalhadorFalso)
    monkeypatch.setattr(main.settings, "RABBITMQ_URL", None)
    with TestClient(main.app) as cliente:
        assert main.app.state.servicos is servicos
        assert cliente.get("/health/ready").status_code == 200
    assert iniciados == []
    monkeypatch.setattr(main.settings, "RABBITMQ_URL", "amqp://x")
    with TestClient(main.app):
        pass
    assert iniciados == [("amqp://x", servicos, main.settings.CONCILIACAO_SEGUNDOS), "start", "parar"]
