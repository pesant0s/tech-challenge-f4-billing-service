"""Montagem de cenários e leitura do outbox, compartilhadas pelos testes."""
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

CONTRATO = Path(os.environ.get(
    "CONTRATO_ASYNCAPI",
    Path(__file__).resolve().parents[2] / "tech-challenge-f4-plataforma/docs/contratos/asyncapi.yaml",
))
AGORA = datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc)


def contrato() -> dict | None:
    return yaml.safe_load(CONTRATO.read_text()) if CONTRATO.exists() else None


def validar_contra_o_contrato(corpo: dict, rota: str) -> None:
    """Sem o repositório da plataforma ao lado, o contrato não está disponível e a conferência é pulada."""
    spec = contrato()
    if spec:
        Draft202012Validator({"$ref": f"#/components/schemas/{corpo['tipo']}", "components": spec["components"]}).validate(corpo)
        enderecos = {tipo: canal["address"] for canal in spec["channels"].values() for tipo in canal["messages"]}
        assert rota == enderecos[corpo["tipo"]]
    assert corpo["origem"] == "billing-service"


def publicadas(bd) -> list[dict]:
    """Corpos do outbox em ordem de gravação; cada um já é conferido contra o AsyncAPI."""
    mensagens = list(bd["outbox"].find())
    for mensagem in mensagens:
        validar_contra_o_contrato(mensagem["corpo"], mensagem["rota"])
    return [m["corpo"] for m in mensagens]


def comando(tipo, os_id, saga_id, payload, message_id=None) -> dict:
    return {"message_id": message_id or str(uuid.uuid4()), "tipo": tipo, "versao": 1, "saga_id": str(saga_id),
            "os_id": str(os_id), "ocorrido_em": "2026-11-12T15:00:00Z", "origem": "os-service", "payload": payload}


def novo_cliente(**dados) -> dict:
    return {"id": str(uuid.uuid4()), "nome": "Ana Souza", "email": "ana@example.com", "telefone": None, **dados}


ITENS = [
    {"tipo": "SERVICO", "item_id": str(uuid.uuid4()), "descricao": "Troca de óleo", "quantidade": 1,
     "preco_unitario": "150.00", "subtotal": "150.00"},
    {"tipo": "PECA", "item_id": str(uuid.uuid4()), "descricao": "Filtro de óleo", "quantidade": 2,
     "preco_unitario": "35.00", "subtotal": "70.00"},
]


def gerar_orcamento(os_id, saga_id, cliente=None, message_id=None) -> dict:
    return comando("GerarOrcamento", os_id, saga_id, {"cliente": cliente or novo_cliente(), "itens": ITENS, "valor_total": "220.00"}, message_id)


def cobrar(os_id, saga_id, orcamento_id) -> dict:
    return comando("Cobrar", os_id, saga_id, {"orcamento_id": orcamento_id})


def cancelar_orcamento(os_id, saga_id, motivo="PAGAMENTO_RECUSADO") -> dict:
    return comando("CancelarOrcamento", os_id, saga_id, {"motivo": motivo})


def reembolsar(os_id, saga_id, motivo="PRAZO_ESGOTADO") -> dict:
    return comando("Reembolsar", os_id, saga_id, {"motivo": motivo})


def orcamento_gerado(servicos, bd, cliente=None, agora=AGORA) -> tuple[dict, dict]:
    """Gera o orçamento de uma OS nova; devolve (envelope do comando, payload do OrcamentoGerado)."""
    envelope = gerar_orcamento(uuid.uuid4(), uuid.uuid4(), cliente)
    servicos.comandos.executar(envelope, agora)
    return envelope, publicadas(bd)[-1]["payload"]


def orcamento_aprovado(servicos, bd, cliente=None, agora=AGORA) -> tuple[dict, str]:
    """Orçamento já aprovado; devolve (envelope do GerarOrcamento, orcamento_id)."""
    envelope, gerado = orcamento_gerado(servicos, bd, cliente, agora)
    servicos.decisao.aprovar(envelope["os_id"], "CLIENTE", None, agora)
    return envelope, gerado["orcamento_id"]


def pagamento_pendente(servicos, bd, agora=AGORA) -> tuple[dict, dict]:
    """Cobrança criada para uma OS nova; devolve (envelope do GerarOrcamento, documento do pagamento)."""
    envelope, orcamento_id = orcamento_aprovado(servicos, bd, agora=agora)
    servicos.comandos.executar(cobrar(envelope["os_id"], envelope["saga_id"], orcamento_id), agora)
    return envelope, bd["pagamentos"].find_one({"orcamento_id": orcamento_id})
