"""Coleções do MongoDB e a unidade de trabalho: estado, outbox e inbox gravados na mesma transação (ADR-021)."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from pymongo import ASCENDING, MongoClient, ReturnDocument
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from app.domain.entities.orcamento import EstadoOrcamento, Orcamento
from app.domain.entities.pagamento import EstadoPagamento, Pagamento
from app.domain.exceptions import ConflictException
from app.infrastructure.rastreamento import cabecalhos_de_rastreamento

RESERVA_DO_OUTBOX = timedelta(seconds=30)


def _atualizar(colecao, sessao, documento: dict, estado_anterior) -> None:
    """Grava só se o estado ainda for o lido: quem perde a corrida não publica o evento."""
    resultado = colecao.replace_one({"_id": documento["_id"], "estado": estado_anterior.value}, documento, session=sessao)
    if resultado.matched_count == 0:
        raise ConflictException("O estado mudou antes da gravação")


class OrcamentoRepositoryAdapter:
    def __init__(self, banco, sessao=None):
        self._colecao = banco["orcamentos"]
        self._sessao = sessao

    @staticmethod
    def _documento(o: Orcamento) -> dict:
        return {"_id": o.id, "os_id": o.os_id, "saga_id": o.saga_id, "cliente": o.cliente, "itens": o.itens,
                "valor_total": str(o.valor_total), "estado": o.estado.value, "expira_em": o.expira_em,
                "aprovado_por": o.aprovado_por, "criado_em": o.criado_em}

    @staticmethod
    def _entidade(d: dict | None) -> Orcamento | None:
        if d is None:
            return None
        return Orcamento(id=d["_id"], os_id=d["os_id"], saga_id=d["saga_id"], cliente=d["cliente"], itens=d["itens"],
                         valor_total=Decimal(d["valor_total"]), estado=EstadoOrcamento(d["estado"]), expira_em=d["expira_em"],
                         aprovado_por=d["aprovado_por"], criado_em=d["criado_em"])

    def adicionar(self, orcamento: Orcamento) -> None:
        self._colecao.insert_one(self._documento(orcamento), session=self._sessao)

    def atualizar(self, orcamento: Orcamento, estado_anterior: EstadoOrcamento) -> None:
        _atualizar(self._colecao, self._sessao, self._documento(orcamento), estado_anterior)

    def buscar(self, orcamento_id: str) -> Orcamento | None:
        return self._entidade(self._colecao.find_one({"_id": orcamento_id}, session=self._sessao))

    def buscar_por_os(self, os_id: str) -> Orcamento | None:
        return self._entidade(self._colecao.find_one({"os_id": os_id}, session=self._sessao))

    def vencidos(self, agora: datetime, depois_de: str, limite: int) -> list[Orcamento]:
        """Orçamentos GERADO com o prazo esgotado, em ordem de id, a partir do último já visto."""
        filtro = {"estado": EstadoOrcamento.GERADO.value, "expira_em": {"$lte": agora}, "_id": {"$gt": depois_de}}
        return [self._entidade(d) for d in self._colecao.find(filtro, session=self._sessao).sort("_id", ASCENDING).limit(limite)]


class PagamentoRepositoryAdapter:
    def __init__(self, banco, sessao=None):
        self._colecao = banco["pagamentos"]
        self._sessao = sessao

    @staticmethod
    def _documento(p: Pagamento) -> dict:
        return {"_id": p.id, "orcamento_id": p.orcamento_id, "os_id": p.os_id, "saga_id": p.saga_id, "provedor": p.provedor,
                "valor": str(p.valor), "estado": p.estado.value, "provedor_ref": p.provedor_ref, "qr_data": p.qr_data,
                "expira_em": p.expira_em, "pago_em": p.pago_em}

    @staticmethod
    def _entidade(d: dict | None) -> Pagamento | None:
        if d is None:
            return None
        return Pagamento(id=d["_id"], orcamento_id=d["orcamento_id"], os_id=d["os_id"], saga_id=d["saga_id"],
                         provedor=d["provedor"], valor=Decimal(d["valor"]), estado=EstadoPagamento(d["estado"]),
                         provedor_ref=d["provedor_ref"], qr_data=d["qr_data"], expira_em=d["expira_em"], pago_em=d["pago_em"])

    def adicionar(self, pagamento: Pagamento) -> None:
        self._colecao.insert_one(self._documento(pagamento), session=self._sessao)

    def atualizar(self, pagamento: Pagamento, estado_anterior: EstadoPagamento) -> None:
        _atualizar(self._colecao, self._sessao, self._documento(pagamento), estado_anterior)

    def buscar_por_orcamento(self, orcamento_id: str) -> Pagamento | None:
        return self._entidade(self._colecao.find_one({"orcamento_id": orcamento_id}, session=self._sessao))

    def buscar_por_provedor_ref(self, provedor_ref: str) -> Pagamento | None:
        return self._entidade(self._colecao.find_one({"provedor_ref": provedor_ref}, session=self._sessao))

    def pendentes(self, depois_de: str, limite: int) -> list[Pagamento]:
        filtro = {"estado": EstadoPagamento.PENDENTE.value, "_id": {"$gt": depois_de}}
        return [self._entidade(d) for d in self._colecao.find(filtro, session=self._sessao).sort("_id", ASCENDING).limit(limite)]


class OutboxRepositoryAdapter:
    def __init__(self, banco, sessao=None):
        self._colecao = banco["outbox"]
        self._sessao = sessao

    def adicionar(self, envelope: dict, exchange: str, rota: str) -> None:
        """Guarda também o contexto do trace atual: o relay publica depois, fora desta transação."""
        self._colecao.insert_one({
            "_id": envelope["message_id"], "exchange": exchange, "rota": rota, "tipo": envelope["tipo"], "corpo": envelope,
            "cabecalhos": cabecalhos_de_rastreamento(), "criado_em": datetime.now(timezone.utc),
            "publicado_em": None, "reservada_ate": None,
        }, session=self._sessao)

    def reivindicar(self, agora: datetime) -> dict | None:
        """Pega a mensagem mais antiga sem dono por 30 s; duas réplicas nunca recebem a mesma."""
        return self._colecao.find_one_and_update(
            {"publicado_em": None, "$or": [{"reservada_ate": None}, {"reservada_ate": {"$lt": agora}}]},
            {"$set": {"reservada_ate": agora + RESERVA_DO_OUTBOX}},
            sort=[("criado_em", ASCENDING)], return_document=ReturnDocument.AFTER,
        )

    def liberar(self, mensagem_id: str) -> None:
        self._colecao.update_one({"_id": mensagem_id}, {"$set": {"reservada_ate": None}})

    def marcar_publicada(self, mensagem_id: str, agora: datetime) -> None:
        self._colecao.update_one({"_id": mensagem_id}, {"$set": {"publicado_em": agora}})


class InboxRepositoryAdapter:
    def __init__(self, banco, sessao=None):
        self._colecao = banco["mensagens_processadas"]
        self._sessao = sessao

    def registrar(self, message_id: str, tipo: str, agora: datetime) -> bool:
        """Grava o message_id; False quando ele já tinha sido processado (e a transação, abortada pelo servidor, é encerrada)."""
        try:
            self._colecao.insert_one({"_id": message_id, "tipo": tipo, "processada_em": agora}, session=self._sessao)
        except DuplicateKeyError:
            if self._sessao:
                self._sessao.abort_transaction()
            return False
        return True


class Repositorios:
    def __init__(self, banco, sessao=None):
        self.orcamentos = OrcamentoRepositoryAdapter(banco, sessao)
        self.pagamentos = PagamentoRepositoryAdapter(banco, sessao)
        self.outbox = OutboxRepositoryAdapter(banco, sessao)
        self.inbox = InboxRepositoryAdapter(banco, sessao)


class Banco:
    def __init__(self, cliente: MongoClient, banco: Database):
        self._cliente = cliente
        self._banco = banco

    def preparar_indices(self) -> None:
        self._banco["orcamentos"].create_index("os_id", unique=True)
        self._banco["orcamentos"].create_index("estado")
        self._banco["pagamentos"].create_index("orcamento_id", unique=True)
        self._banco["pagamentos"].create_index("provedor_ref")
        self._banco["pagamentos"].create_index("estado")
        self._banco["outbox"].create_index([("publicado_em", ASCENDING), ("criado_em", ASCENDING)])

    def ping(self) -> None:
        self._cliente.admin.command("ping")

    def leitura(self) -> Repositorios:
        """Acesso fora de transação, para consultas e para o relay do outbox."""
        return Repositorios(self._banco)

    @contextmanager
    def transacao(self):
        """Confirma ao sair do bloco e desfaz se houver exceção."""
        with self._cliente.start_session() as sessao, sessao.start_transaction():
            yield Repositorios(self._banco, sessao)
