"""Thread de mensageria do processo: consome comandos, publica o outbox e concilia pagamentos, com reconexão."""
import logging
import threading
import time
from functools import partial

import pika

from app.adapters.inbound.mensageria import tratar_mensagem
from app.adapters.outbound.mensageria import FILA_COMANDOS, declarar_topologia, publicar_pendentes

logger = logging.getLogger("oficina.mensageria")


class TrabalhadorMensageria(threading.Thread):
    def __init__(self, url: str, servicos, conciliacao_segundos: float):
        super().__init__(name="mensageria", daemon=True)
        self._url = url
        self._servicos = servicos
        self._intervalo = conciliacao_segundos
        self._parar = threading.Event()

    def parar(self) -> None:
        self._parar.set()
        self.join(timeout=5)

    def run(self) -> None:
        espera = 1
        while not self._parar.is_set():
            try:
                self._trabalhar()
                espera = 1
            except Exception as exc:  # broker ou banco fora do ar: a thread não pode morrer
                logger.warning("Mensageria interrompida (%s); nova tentativa em %ss", exc, espera,
                               extra={"evento": "mensageria_reconectando"})
                self._parar.wait(espera)
                espera = min(espera * 2, 30)

    def _trabalhar(self) -> None:
        conexao = pika.BlockingConnection(pika.URLParameters(self._url))
        try:
            canal = conexao.channel()
            declarar_topologia(canal)
            canal.confirm_delivery()
            canal.basic_qos(prefetch_count=10)
            canal.basic_consume(FILA_COMANDOS, partial(tratar_mensagem, comandos=self._servicos.comandos))
            logger.info("Mensageria conectada", extra={"evento": "mensageria_conectada"})
            proxima_conciliacao = 0.0
            while not self._parar.is_set():
                conexao.process_data_events(time_limit=1)
                publicar_pendentes(canal, self._servicos.banco)
                if time.monotonic() >= proxima_conciliacao:
                    self._servicos.conciliacao.executar()
                    proxima_conciliacao = time.monotonic() + self._intervalo
        finally:
            if conexao.is_open:
                conexao.close()
