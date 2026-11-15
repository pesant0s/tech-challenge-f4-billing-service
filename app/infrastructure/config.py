from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    MONGO_URL: str
    MONGO_DB: str = "billing"
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ALLOWED_ORIGINS: list[str] = ["http://localhost:3000"]
    LOG_FORMAT: str = "console"
    LOG_LEVEL: str = "INFO"
    # Atrás do API Gateway o Swagger sai em /billing/docs, sem colidir com o de outros serviços.
    PREFIXO_DOCS: str = ""
    # Sem RABBITMQ_URL a mensageria não sobe: a API atende e os eventos ficam parados no outbox.
    RABBITMQ_URL: str | None = None
    PAGAMENTO_PROVEDOR: str = "simulado"
    MP_API_URL: str = "https://api.mercadopago.com"
    MP_ACCESS_TOKEN: str | None = None
    MP_EXTERNAL_POS_ID: str | None = None
    MP_WEBHOOK_SECRET: str | None = None
    PAGAMENTO_EXPIRACAO_MINUTOS: int = 15
    ORCAMENTO_VALIDADE_MINUTOS: int = 1440
    CONCILIACAO_SEGUNDOS: int = 15


settings = Settings()
