import logging
import sys
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from app.infrastructure.config import settings
from app.infrastructure.logging_config import ConsoleFormatter, JsonFormatter, obter_correlation_id
from app.infrastructure.composicao import montar_servicos
from app.infrastructure.trabalhador import TrabalhadorMensageria
from app.adapters.inbound.http.dependencias import obter_servicos
from app.adapters.inbound.http.middleware import CorrelationIdMiddleware, SecurityHeadersMiddleware
from app.adapters.inbound.http.orcamento_routes import router as orcamento_router
from app.adapters.inbound.http.webhook_routes import router as webhook_router
from app.domain.exceptions import NotFoundException, ConflictException, BusinessRuleException

# Configurado no import para que os logs de startup já saiam no formato final.
_saida = logging.StreamHandler(sys.stdout)
_saida.setFormatter(JsonFormatter() if settings.LOG_FORMAT.lower() == "json" else ConsoleFormatter())
logging.basicConfig(handlers=[_saida], level=settings.LOG_LEVEL.upper(), force=True)
for _nome in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    logging.getLogger(_nome).handlers.clear()
    logging.getLogger(_nome).propagate = True
logging.getLogger("pika").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.servicos = montar_servicos(settings)
    trabalhador = None
    if settings.RABBITMQ_URL:
        trabalhador = TrabalhadorMensageria(settings.RABBITMQ_URL, app.state.servicos, settings.CONCILIACAO_SEGUNDOS)
        trabalhador.start()
    try:
        yield
    finally:
        if trabalhador:
            trabalhador.parar()


app = FastAPI(
    title="Tech Challenge — Oficina · Billing",
    description="""
Serviço de orçamento e pagamento da oficina. O orçamento é gerado e a cobrança é criada por comandos da
Saga da OS pelo RabbitMQ; o cliente (ou a oficina) aprova o orçamento e acompanha o pagamento pelas
rotas abaixo. O pagamento é cobrado por QR Code dinâmico, pelo Mercado Pago ou por um simulador.

## Autenticação

Token emitido pelo OS Service (funcionário) ou pela Lambda de autenticação (cliente), enviado como
`Authorization: Bearer <token>`. Aqui só se confere a assinatura e os claims. O cliente acessa apenas o
orçamento da própria OS.

## Orçamento

`GERADO` → `APROVADO`, `REJEITADO`, `EXPIRADO` ou `CANCELADO`. Cada decisão publica o evento correspondente.

## Acesso público

| Rota | Descrição |
|---|---|
| `GET /health` | Liveness |
| `GET /health/ready` | Readiness — confirma o MongoDB |
| `POST /billing/webhooks/mercadopago` | Notificação do Mercado Pago, validada pela assinatura |
    """,
    version="1.0.0",
    lifespan=lifespan,
    swagger_ui_parameters={"persistAuthorization": True},
    docs_url=f"{settings.PREFIXO_DOCS}/docs",
    redoc_url=f"{settings.PREFIXO_DOCS}/redoc",
    openapi_url=f"{settings.PREFIXO_DOCS}/openapi.json",
)

def _erro(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail, "correlation_id": obter_correlation_id()})


@app.exception_handler(NotFoundException)
async def not_found_handler(request: Request, exc: NotFoundException):
    return _erro(404, exc.detail)


@app.exception_handler(ConflictException)
async def conflict_handler(request: Request, exc: ConflictException):
    return _erro(409, exc.detail)


@app.exception_handler(BusinessRuleException)
async def business_rule_handler(request: Request, exc: BusinessRuleException):
    logging.getLogger("oficina.negocio").warning("Regra de negócio violada: %s", exc.detail, extra={"regra": exc.detail})
    return _erro(422, exc.detail)


# O último middleware registrado é o mais externo: a correlação envolve todos os outros.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=False,  # o token vai no cabeçalho, não em cookie
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(CorrelationIdMiddleware)

app.include_router(orcamento_router)
app.include_router(webhook_router)


@app.get("/health", tags=["Sistema"], summary="Liveness")
def health():
    """Sem I/O: uma queda do banco não deve fazer o Kubernetes reiniciar pods saudáveis."""
    return {"status": "ok", "version": "1.0.0"}


@app.get("/health/ready", tags=["Sistema"], summary="Readiness")
def health_ready(servicos=Depends(obter_servicos)):
    """Confere o MongoDB; sem ele o pod sai do balanceamento, mas segue vivo."""
    try:
        servicos.banco.ping()
    except Exception as exc:
        logging.getLogger("oficina.health").error("Readiness falhou: banco inacessível", exc_info=exc)
        return JSONResponse(status_code=503, content={
            "status": "degradado", "banco": "inacessivel", "correlation_id": obter_correlation_id(),
        })
    return {"status": "ok", "banco": "ok", "version": "1.0.0"}
