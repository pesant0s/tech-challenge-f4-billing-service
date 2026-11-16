# tech-challenge-f4-billing-service

Serviço de **orçamento e pagamento** da oficina mecânica. Gera o orçamento da ordem de serviço (OS), registra a
aprovação ou a rejeição do cliente, cobra por QR Code dinâmico do Mercado Pago, concilia o pagamento e executa o
cancelamento e o reembolso comandados pela Saga da OS. Banco próprio em MongoDB, comunicação com os outros serviços
por mensagens no RabbitMQ.

[![Quality Gate](https://sonarcloud.io/api/project_badges/measure?project=pesant0s_tech-challenge-f4-billing-service&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=pesant0s_tech-challenge-f4-billing-service)
[![Coverage](https://sonarcloud.io/api/project_badges/measure?project=pesant0s_tech-challenge-f4-billing-service&metric=coverage)](https://sonarcloud.io/summary/new_code?id=pesant0s_tech-challenge-f4-billing-service)

> Pós-Tech Software Architecture · FIAP · Tech Challenge, Fase 04

| | |
|---|---|
| **Papel** | Orçamento, cobrança, conciliação, cancelamento e reembolso da OS |
| **Linguagem e framework** | Python 3.12, FastAPI 0.111, Pydantic 2.7 |
| **Banco** | MongoDB 8.0 em replica set de 1 nó (o NoSQL da solução) |
| **Mensageria** | RabbitMQ, cliente `pika` 1.3 |
| **Pagamento** | Mercado Pago (Orders API, QR Code dinâmico) ou provedor simulado |
| **Runtime** | Amazon EKS, namespace `billing-service`, 2 a 4 réplicas |
| **Exposição** | API Gateway → VPC Link → NLB interno (listener 8082) → NodePort 30082 → pods (porta 8000) |
| **Porta local** | 8003 (docker compose) |
| **Autenticação** | Valida o JWT emitido pelo OS Service (funcionário) ou pela Lambda (cliente) |
| **Observabilidade** | New Relic (APM, logs JSON, traces que atravessam o RabbitMQ) |

## Os cinco repositórios

A solução da Fase 04 está dividida em cinco repositórios independentes, cada um com pipeline próprio.

| Repositório | Papel |
|---|---|
| [`tech-challenge-f4-plataforma`](https://github.com/pesant0s/tech-challenge-f4-plataforma) | Terraform da base compartilhada (rede, EKS, NLB, API Gateway, RabbitMQ, New Relic), contratos das mensagens (AsyncAPI), ADRs, coleção Postman e ambiente local |
| [`tech-challenge-f4-os-service`](https://github.com/pesant0s/tech-challenge-f4-os-service) | Clientes, veículos, usuários, login e OS; orquestrador da Saga; PostgreSQL |
| [`tech-challenge-f4-execucao-service`](https://github.com/pesant0s/tech-challenge-f4-execucao-service) | Catálogo, estoque, reserva de peças, fila de execução, diagnóstico e reparo; PostgreSQL |
| **tech-challenge-f4-billing-service** (este) | Orçamento e pagamento pelo Mercado Pago; MongoDB |
| [`tech-challenge-f4-auth-lambda`](https://github.com/pesant0s/tech-challenge-f4-auth-lambda) | Lambda `tech-challenge-f4-auth`: autenticação do cliente por CPF (`POST /auth/cpf`) |

## Sumário

- [Visão geral do sistema](#visão-geral-do-sistema)
- [Arquitetura do serviço](#arquitetura-do-serviço)
- [Responsabilidades e limites](#responsabilidades-e-limites)
- [Modelo de dados](#modelo-de-dados)
- [Mensageria](#mensageria)
- [Papel na Saga](#papel-na-saga)
- [Mercado Pago](#mercado-pago)
- [API REST](#api-rest)
- [Configuração](#configuração)
- [Como rodar localmente](#como-rodar-localmente)
- [Testes e cobertura](#testes-e-cobertura)
- [Pipeline CI/CD](#pipeline-cicd)
- [Infraestrutura própria](#infraestrutura-própria)
- [Kubernetes](#kubernetes)
- [Deploy na AWS](#deploy-na-aws)
- [Observabilidade](#observabilidade)
- [Decisões de arquitetura](#decisões-de-arquitetura)
- [Limitações conhecidas](#limitações-conhecidas)

---

## Visão geral do sistema

O OS Service orquestra a Saga de abertura da OS e troca mensagens com a Execução e com o Billing. O Billing
é o único componente que fala com o Mercado Pago. O cliente e a oficina chegam a ele pelo API Gateway, na rota
`/billing/{proxy+}`.

```mermaid
flowchart LR
  U["Funcionário / Cliente"]
  MP["Mercado Pago<br/>Orders API · QR dinâmico"]
  NR["New Relic<br/>APM · logs · traces · dashboards"]
  GH["GitHub Actions<br/>testes · SonarQube · deploy"]
  subgraph AWS["AWS · us-east-1"]
    APIGW["API Gateway HTTP<br/>rota por prefixo"]
    L["Lambda tech-challenge-f4-auth<br/>POST /auth/cpf"]
    ECR["ECR · um por serviço"]
    SSM["SSM Parameter Store<br/>/tech-challenge-f4/*"]
    subgraph VPC["VPC"]
      NLB["NLB interno<br/>80 · 8081 · 8082"]
      subgraph EKS["EKS · 3 nós t3.medium"]
        OS["OS Service<br/>orquestrador da Saga"]
        EX["Execução e Produção"]
        BI["Billing"]
        RMQ["RabbitMQ<br/>namespace mensageria"]
        MDB[("MongoDB rs0<br/>namespace billing-service")]
      end
      RDS1[("RDS PostgreSQL<br/>os-service")]
      RDS2[("RDS PostgreSQL<br/>execucao-service")]
    end
  end
  U --> APIGW
  APIGW -->|"POST /auth/cpf"| L
  APIGW -->|"VPC Link"| NLB
  L -->|"/interno/clientes/autenticar"| NLB
  NLB -->|"NodePort 30080"| OS
  NLB -->|"NodePort 30081"| EX
  NLB -->|"NodePort 30082"| BI
  OS --- RDS1
  EX --- RDS2
  BI --- MDB
  OS <-->|"comandos e eventos"| RMQ
  EX <--> RMQ
  BI <--> RMQ
  BI -->|"cobrança · consulta · cancelamento · reembolso"| MP
  MP -.->|"webhook /billing/webhooks/mercadopago"| APIGW
  EKS -.->|"agente"| NR
  GH -->|"imagem"| ECR
  GH -->|"deploy"| EKS
  style BI stroke-width:4px
```

A visão completa do sistema, o contrato de infraestrutura e a ordem de subida estão no
[README da plataforma](https://github.com/pesant0s/tech-challenge-f4-plataforma#readme).

Rotas do API Gateway, sem reescrita de caminho:

| Rota | Destino |
|---|---|
| `ANY /auth/{proxy+}`, `/cadastro/{proxy+}`, `/atendimento/{proxy+}`, `GET /docs`, `/openapi.json`, `/redoc`, `/health` | os-service |
| `ANY /catalogo/{proxy+}`, `/estoque/{proxy+}`, `/execucao/{proxy+}` | execucao-service |
| `ANY /billing/{proxy+}` | **billing-service** (este) |
| `POST /auth/cpf` | Lambda de autenticação |

A rota `/interno` não existe no gateway. O `GET /health` do gateway é do OS Service: o `/health` e o
`/health/ready` do Billing só são acessíveis de dentro do cluster (probes do Kubernetes e health check do NLB).

---

## Arquitetura do serviço

Arquitetura hexagonal. O domínio não conhece FastAPI, MongoDB, RabbitMQ nem Mercado Pago. Os adapters de entrada
chamam casos de uso; os casos de uso usam as entidades e as portas do domínio; os adapters de saída implementam as
portas.

```mermaid
flowchart LR
  subgraph IN["Adapters de entrada"]
    HTTP["HTTP · FastAPI<br/>orcamento_routes · webhook_routes<br/>middleware · dependencias"]
    MSG["Mensageria · inbound<br/>tratar_mensagem"]
    TRAB["TrabalhadorMensageria<br/>thread de cada processo"]
  end
  subgraph APP["Application · use_cases"]
    UC1["ProcessarComandoUseCase"]
    UC2["DecidirOrcamentoUseCase"]
    UC3["ConciliarPagamentosUseCase"]
    UC4["ProcessarNotificacaoMercadoPagoUseCase"]
  end
  subgraph DOM["Domain"]
    ENT["Orcamento · Pagamento<br/>estados e transições"]
    PORTAS["Portas<br/>ProvedorPagamento<br/>NotificacaoOrcamento"]
  end
  subgraph OUT["Adapters de saída"]
    DB["persistence · Banco<br/>repositórios, transação, outbox, inbox"]
    PAG["pagamento<br/>mercadopago · simulado"]
    NOT["notifications<br/>notificacao_simulada"]
    REL["mensageria · outbound<br/>topologia e relay do outbox"]
  end
  MONGO[("MongoDB")]
  MPAPI["Mercado Pago"]
  BROKER["RabbitMQ"]
  HTTP --> UC2
  HTTP --> UC4
  MSG --> UC1
  TRAB --> MSG
  TRAB --> UC3
  TRAB --> REL
  UC4 --> UC3
  UC1 --> ENT
  UC2 --> ENT
  UC3 --> ENT
  UC1 --> PORTAS
  UC3 --> PORTAS
  PAG -.->|"implementa"| PORTAS
  NOT -.->|"implementa"| PORTAS
  UC1 --> DB
  UC2 --> DB
  UC3 --> DB
  UC4 --> DB
  REL --> DB
  DB --> MONGO
  PAG --> MPAPI
  BROKER --> MSG
  REL --> BROKER
```

Pacotes de `app/`:

| Pacote | Conteúdo |
|---|---|
| `app/main.py` | Aplicação FastAPI, ciclo de vida (monta os serviços e sobe a thread de mensageria), tratadores de exceção, middlewares, `/health` e `/health/ready` |
| `app/domain/entities/` | `Orcamento` e `Pagamento`: estados, transições e regras (prazo, quem pode mudar de estado) |
| `app/domain/ports/` | `ProvedorPagamento` (e `SituacaoPagamento`, `Cobranca`, `FalhaNoProvedor`) e `NotificacaoOrcamento` |
| `app/domain/dinheiro.py`, `exceptions.py` | Formato monetário do contrato (texto com duas casas) e exceções de negócio (`NotFoundException`, `ConflictException`, `BusinessRuleException`) |
| `app/application/use_cases/` | `comandos_saga.py` (`ProcessarComandoUseCase`), `decidir_orcamento.py`, `conciliar_pagamentos.py`, `processar_notificacao_mercadopago.py` |
| `app/application/mensagens.py` | Montagem do envelope e publicação de evento no outbox |
| `app/adapters/inbound/http/` | Rotas (`orcamento_routes.py`, `webhook_routes.py`), schemas de resposta, middlewares de correlação e de cabeçalhos de segurança, dependências |
| `app/adapters/inbound/mensageria.py` | Validação do envelope, ack e nack da mensagem consumida |
| `app/adapters/outbound/persistence/banco.py` | Repositórios das coleções, unidade de trabalho transacional, índices |
| `app/adapters/outbound/pagamento/` | `mercadopago.py` e `simulado.py`, as duas implementações da porta de pagamento |
| `app/adapters/outbound/notifications/` | `notificacao_simulada.py`: registra o envio do orçamento em log |
| `app/adapters/outbound/mensageria.py` | Topologia do RabbitMQ e relay do outbox |
| `app/infrastructure/` | `config.py` (variáveis), `composicao.py` (monta as dependências), `trabalhador.py` (thread de mensageria), `security.py` (JWT), `logging_config.py`, `rastreamento.py` (trace pela fila) |

Observações de projeto:

- As portas formais do domínio são `ProvedorPagamento` e `NotificacaoOrcamento`. Os casos de uso recebem o adapter de
  persistência (`Banco`) por injeção, sem uma porta própria.
- A montagem das dependências fica em `composicao.py`. O Mercado Pago só é instanciado quando
  `PAGAMENTO_PROVEDOR=mercadopago`; sem `MP_ACCESS_TOKEN` e `MP_EXTERNAL_POS_ID` o serviço se recusa a subir.
- Cada processo do Uvicorn (`UVICORN_WORKERS`, padrão 2) executa a API e uma thread de mensageria própria. Com 2
  réplicas há, portanto, quatro consumidores, relays e conciliadores concorrentes. A correção não depende de
  exclusividade: o outbox é reivindicado com reserva, a inbox tem chave única e toda mudança de estado é condicional
  (veja [Modelo de dados](#modelo-de-dados)).
- Chamadas ao provedor de pagamento acontecem fora da transação do MongoDB. A idempotência vem das chaves
  `X-Idempotency-Key` derivadas dos ids.

Estrutura do repositório:

```text
.
├── app/                              # código do serviço (pacotes acima)
├── tests/                            # suíte pytest (187 testes)
├── scripts/mercadopago_preparar.py   # cria a loja e o caixa no Mercado Pago
├── docs/modelo-de-dados.md           # coleções, índices e estados
├── k8s/base e k8s/overlays/producao  # manifestos Kubernetes
├── infra/                            # Terraform do ECR deste serviço
├── .github/workflows/ci-cd.yml       # pipeline
├── Dockerfile · entrypoint.sh · docker-compose.yml · Makefile
└── .env.example · requirements.txt · sonar-project.properties
```

Imagem Docker: build em dois estágios sobre `python:3.12-slim` (as dependências vão para um virtualenv em `/venv`),
usuário não root de UID 10001, `curl` para o `HEALTHCHECK` (`GET /health`), porta 8000. O `entrypoint.sh` sobe o
Uvicorn com `UVICORN_WORKERS` processos (padrão 2) e `--no-access-log`; com `NEW_RELIC_LICENSE_KEY` definida,
envolve o comando em `newrelic-admin run-program`.

---

## Responsabilidades e limites

**O Billing faz:**

- gerar o orçamento da OS a partir dos itens já precificados que o OS Service envia, com validade
  (`ORCAMENTO_VALIDADE_MINUTOS`, padrão 1440 minutos);
- avisar o cliente do orçamento (aviso simulado, registrado em log);
- receber a aprovação ou a rejeição do orçamento pela API, do cliente titular ou de um funcionário;
- expirar o orçamento que passou da validade sem decisão;
- criar a cobrança por QR Code dinâmico no Mercado Pago (ou no simulador) quando o OS Service manda `Cobrar`;
- conciliar o pagamento: periodicamente e a cada notificação do webhook;
- cancelar o orçamento e a cobrança pendente, e reembolsar o pagamento confirmado, quando a Saga compensa;
- publicar cada resultado como evento, de forma transacional (outbox).

**O Billing não faz:**

- não decide o status da OS nem o rumo da Saga: publica fatos (aprovado, rejeitado, pago, recusado) e o OS Service
  decide;
- não cadastra cliente, veículo, serviço nem peça, e não reserva estoque: os preços e as quantidades chegam prontos no
  comando `GerarOrcamento`;
- não emite token: só confere a assinatura e os claims do JWT;
- não envia e-mail nem SMS: o aviso do orçamento é registrado em log (`NotificacaoSimulada`);
- não cria uma segunda cobrança para o mesmo orçamento: há um pagamento por orçamento;
- não faz reembolso parcial: o reembolso é do valor total.

**De quem é cada dado:**

| Dado | Dono | Como o Billing o obtém |
|---|---|---|
| Orçamento, itens, estado e validade | Billing (`orcamentos`) | Criado pelo comando `GerarOrcamento` |
| Pagamento, QR Code e situação | Billing (`pagamentos`) | Criado pelo comando `Cobrar`; atualizado pela conciliação |
| Mensagens a publicar e já processadas | Billing (`outbox`, `mensagens_processadas`) | Gravadas na mesma transação do efeito |
| Cliente (id, nome, e-mail, telefone) | OS Service | Cópia no comando `GerarOrcamento`; o id define o titular do orçamento. CPF e CNPJ não trafegam |
| OS e seu status | OS Service | Só o `os_id` e o `saga_id` chegam nas mensagens |
| Preço e reserva das peças | Execução | Vêm precificados nos itens de `GerarOrcamento` |
| Credenciais de usuário | OS Service | O JWT é conferido com a mesma `SECRET_KEY` |

Nenhum serviço acessa o banco de outro. O MongoDB do Billing só é lido e escrito por este serviço.

---

## Modelo de dados

Banco MongoDB `billing` (`MONGO_DB`). Detalhes e diagramas também em [`docs/modelo-de-dados.md`](docs/modelo-de-dados.md).

**Por que documento.** O orçamento e o pagamento são agregados fechados por OS: o orçamento carrega dentro de si o
cliente e a lista de itens, e o pagamento carrega o QR Code e as referências do provedor. Tudo o que o serviço lê e
grava ao tratar um comando ou uma decisão está em um documento, localizado por `os_id` (orçamento) ou por
`orcamento_id` (pagamento), sem junções. A idempotência vem de operações condicionais sobre um documento:
`replace_one` com filtro `{_id, estado: <estado lido>}` e `insert_one` em coleção com `_id` ou índice único. Quem perde
a corrida entre duas réplicas, ou entre a conciliação e o webhook, não casa o filtro, recebe `ConflictException`, tem
a transação desfeita e não publica o evento.

**Por que replica set de 1 nó.** Cada comando e cada decisão gravam três coisas juntas: a mudança de estado, o evento
no `outbox` e o `message_id` na `mensagens_processadas`. Isso é uma transação com vários documentos, e o MongoDB só a
aceita em replica set, mesmo com um nó. O docker compose, o CI e o cluster sobem o MongoDB com `--replSet rs0`, e o
replica set é iniciado com `rs.initiate` no primeiro health check. O nó se anuncia como `localhost:27017`, por isso as
conexões de dentro do compose e do cluster usam `directConnection=true`, em vez de seguir o anúncio. A partir do host,
`replicaSet=rs0` também funciona, porque `localhost:27017` é alcançável.

```mermaid
erDiagram
    orcamentos ||--o| pagamentos : "é cobrado em"
    orcamentos ||--o{ outbox : "gera eventos"
    pagamentos ||--o{ outbox : "gera eventos"
    pagamentos |o--o| simulador_pagamentos : "provedor simulado"

    orcamentos {
        string _id "uuid do orçamento"
        string os_id UK
        string saga_id
        object cliente "id, nome, email, telefone"
        array itens "itens precificados do contrato"
        string valor_total
        string estado "GERADO, APROVADO, REJEITADO, EXPIRADO, CANCELADO"
        date expira_em
        string aprovado_por "CLIENTE ou FUNCIONARIO"
        date criado_em
    }
    pagamentos {
        string _id "uuid5 de pagamento:orcamento_id"
        string orcamento_id UK
        string os_id
        string saga_id
        string provedor "MERCADO_PAGO ou SIMULADO"
        string valor
        string estado "PENDENTE, CONFIRMADO, RECUSADO, EXPIRADO, CANCELADO, REEMBOLSADO, REEMBOLSO_FALHOU"
        string provedor_ref "id do pedido no provedor"
        string qr_data
        date expira_em
        date pago_em
    }
    outbox {
        string _id "message_id"
        string exchange
        string rota
        string tipo
        object corpo "envelope completo"
        object cabecalhos "contexto do trace"
        date criado_em
        date publicado_em "nulo até o broker confirmar"
        date reservada_ate "reserva de 30 s do relay"
    }
    mensagens_processadas {
        string _id "message_id do comando"
        string tipo
        date processada_em
    }
    simulador_pagamentos {
        string _id "provedor_ref"
        string situacao
    }
```

### Coleções

| Coleção | Papel | Campos principais |
|---|---|---|
| `orcamentos` | Um por OS | `_id` (uuid), `os_id`, `saga_id`, `cliente`, `itens`, `valor_total` (texto), `estado`, `expira_em`, `aprovado_por`, `criado_em` |
| `pagamentos` | Um por orçamento cobrado | `_id` (uuid5 de `pagamento:<orcamento_id>`), `orcamento_id`, `os_id`, `saga_id`, `provedor`, `valor` (texto), `estado`, `provedor_ref`, `qr_data`, `expira_em`, `pago_em` |
| `outbox` | Eventos a publicar em `oficina.eventos` | `_id` (= `message_id`), `exchange`, `rota`, `tipo`, `corpo` (envelope), `cabecalhos` (contexto do trace), `criado_em`, `publicado_em`, `reservada_ate` |
| `mensagens_processadas` | Inbox: comandos já tratados | `_id` (= `message_id`), `tipo`, `processada_em` |
| `simulador_pagamentos` | Situação das cobranças do provedor simulado | `_id` (= `provedor_ref`), `situacao` |

Dinheiro é texto com duas casas (`"150.00"`), como no contrato. As datas são gravadas em UTC.

### Índices e restrições

Criados na subida do serviço (`Banco.preparar_indices`).

| Coleção | Índice | Motivo |
|---|---|---|
| `orcamentos` | `os_id`, único | Um orçamento por OS; consulta pela rota e pelos comandos |
| `orcamentos` | `estado` | Busca dos orçamentos vencidos na conciliação |
| `pagamentos` | `orcamento_id`, único | Um pagamento por orçamento |
| `pagamentos` | `provedor_ref` | Localizar o pagamento a partir do pedido do Mercado Pago (webhook) |
| `pagamentos` | `estado` | Busca dos pagamentos pendentes na conciliação |
| `outbox` | `(publicado_em, criado_em)` | O relay lê as mensagens pendentes em ordem de criação |
| `mensagens_processadas`, `outbox`, `simulador_pagamentos` | `_id` | O `_id` já é único: `message_id` ou `provedor_ref` |

O `_id` do pagamento é derivado do orçamento (`uuid5`). Repetir a cobrança de um orçamento reaproveita a mesma chave
de idempotência no Mercado Pago.

### Estados

```mermaid
stateDiagram-v2
    direction LR
    state "Orçamento" as O {
        [*] --> GERADO
        GERADO --> APROVADO
        GERADO --> REJEITADO
        GERADO --> EXPIRADO
        GERADO --> CANCELADO
        APROVADO --> CANCELADO
    }
    state "Pagamento" as P {
        [*] --> PENDENTE
        PENDENTE --> CONFIRMADO
        PENDENTE --> RECUSADO
        PENDENTE --> EXPIRADO
        PENDENTE --> CANCELADO
        CONFIRMADO --> REEMBOLSADO
        CONFIRMADO --> REEMBOLSO_FALHOU
    }
```

Regras do domínio (`app/domain/entities/`):

- só um orçamento `GERADO` é aprovado, rejeitado ou expirado; aprovar depois de `expira_em` é erro de regra (HTTP 422);
- expirar antes de `expira_em` é erro de regra;
- cancelar altera apenas orçamento `GERADO` ou `APROVADO`; nos demais estados não faz nada;
- só um pagamento `PENDENTE` é confirmado, recusado, expirado ou cancelado; só um `CONFIRMADO` é reembolsado ou
  marcado com falha de reembolso.

---

## Mensageria

O Billing consome uma fila e publica em uma exchange. O contrato completo (AsyncAPI 3.0) está em
[`docs/contratos/asyncapi.yaml`](https://github.com/pesant0s/tech-challenge-f4-plataforma/blob/main/docs/contratos/asyncapi.yaml)
na plataforma; um resumo está em
[`docs/contratos/README.md`](https://github.com/pesant0s/tech-challenge-f4-plataforma/blob/main/docs/contratos/README.md).

### Topologia

| Item | Valor |
|---|---|
| Exchanges (todas `topic`, duráveis) | `oficina.comandos`, `oficina.eventos`, `oficina.dlx` |
| Fila de comandos | `billing-service.comandos`, quorum queue, ligada a `oficina.comandos` pela chave `billing.#` |
| Limite de entregas | `x-delivery-limit: 5` (a primeira entrega e cinco reentregas) |
| DLQ | `billing-service.comandos.dlq`, quorum queue, ligada a `oficina.dlx` pela chave `billing-service.comandos` |
| Publicação | exchange `oficina.eventos`, chave `billing.<nome-em-kebab-case>` |

O Billing declara as exchanges e as filas ao conectar. A declaração é idempotente: a ordem de subida dos serviços não
importa.

### Envelope

Toda mensagem, de comando ou de evento, tem o mesmo corpo JSON (exemplo com valores ilustrativos):

```json
{
  "message_id": "b0b8a3d2-8a52-4f4e-9d0c-3f0b7b1d9a11",
  "tipo": "CobrancaCriada",
  "versao": 1,
  "saga_id": "6c1d1f0e-5a6f-4a45-a1c3-0f1f4f1e7b20",
  "os_id": "66fb4f6f-a4e9-4ce7-a246-2c46ff9b4af3",
  "ocorrido_em": "2025-01-15T12:00:00Z",
  "origem": "billing-service",
  "payload": {
    "pagamento_id": "495e90f0-42b9-5d27-95c5-ace7d46d2e4e",
    "provedor": "MERCADO_PAGO",
    "valor": "150.00",
    "qr_data": "00020101…",
    "expira_em": "2025-01-15T12:15:00Z"
  }
}
```

| Propriedade AMQP | Valor |
|---|---|
| `message_id` | igual ao `message_id` do corpo |
| `type` | igual ao `tipo` |
| `correlation_id` | `saga_id` |
| `content_type` | `application/json` |
| `delivery_mode` | 2 (persistente) |
| cabeçalhos | `traceparent`, `tracestate` e `newrelic`, para o rastreamento distribuído (vazios sem o agente ativo) |

O contexto de trace vai nos cabeçalhos AMQP, não no corpo.

### Comandos consumidos

Fila `billing-service.comandos`. Cada comando é aplicado no máximo uma vez por `message_id` (inbox).

| Comando | Chave de roteamento | Payload | O que o Billing faz | Eventos de resposta |
|---|---|---|---|---|
| `GerarOrcamento` | `billing.gerar-orcamento` | `cliente` (`id`, `nome`, `email`, `telefone`), `itens` precificados, `valor_total` | Cria o orçamento `GERADO` com validade `ORCAMENTO_VALIDADE_MINUTOS` e registra o aviso ao cliente. Se a OS já tem orçamento, não duplica e responde de novo | `OrcamentoGerado` |
| `Cobrar` | `billing.cobrar` | `orcamento_id` | Se o orçamento está `APROVADO` e ainda não tem pagamento, cria a cobrança no provedor e grava o pagamento `PENDENTE`. Se já existe pagamento, responde de novo com a mesma cobrança | `CobrancaCriada`, ou `PagamentoRecusado` (motivo `PAGAMENTO_RECUSADO`, `pagamento_id` vazio) quando o orçamento não está aprovado |
| `CancelarOrcamento` | `billing.cancelar-orcamento` | `motivo` | Cancela no provedor a cobrança pendente e marca pagamento e orçamento como `CANCELADO`. Se o provedor responde que o pedido já foi pago, confirma o pagamento em vez de cancelar | `OrcamentoCancelado`, precedido de `PagamentoConfirmado` quando o pagamento já tinha sido feito |
| `Reembolsar` | `billing.reembolsar` | `motivo` | Pede o reembolso total ao provedor e marca o pagamento `CONFIRMADO` como `REEMBOLSADO`. Se o pagamento não está `CONFIRMADO` (ou não existe), responde sem tocar no provedor | `PagamentoReembolsado`, ou `ReembolsoFalhou` quando o provedor recusa |

O campo `motivo` de `CancelarOrcamento` e de `Reembolsar` faz parte do contrato, mas o Billing não o usa.

### Eventos publicados

Exchange `oficina.eventos`.

| Evento | Chave de roteamento | Payload | Quando |
|---|---|---|---|
| `OrcamentoGerado` | `billing.orcamento-gerado` | `orcamento_id`, `valor_total`, `expira_em` | Ao tratar `GerarOrcamento` |
| `OrcamentoAprovado` | `billing.orcamento-aprovado` | `orcamento_id`, `aprovado_por` (`CLIENTE` ou `FUNCIONARIO`) | `POST /billing/orcamentos/{os_id}/aprovar` |
| `OrcamentoRejeitado` | `billing.orcamento-rejeitado` | `orcamento_id`, `motivo` (`ORCAMENTO_REJEITADO` ou `ORCAMENTO_EXPIRADO`) | `POST .../rejeitar` (motivo `ORCAMENTO_REJEITADO`) e conciliação de orçamento vencido (motivo `ORCAMENTO_EXPIRADO`) |
| `CobrancaCriada` | `billing.cobranca-criada` | `pagamento_id`, `provedor` (`MERCADO_PAGO` ou `SIMULADO`), `valor`, `qr_data`, `expira_em` | Ao tratar `Cobrar` |
| `PagamentoConfirmado` | `billing.pagamento-confirmado` | `pagamento_id`, `valor`, `pago_em` | Conciliação ou webhook que encontra o pedido pago; `CancelarOrcamento` que encontra o pagamento já feito |
| `PagamentoRecusado` | `billing.pagamento-recusado` | `pagamento_id`, `motivo` (`PAGAMENTO_RECUSADO` ou `PAGAMENTO_EXPIRADO`) | Conciliação de pedido recusado ou expirado; `Cobrar` sem orçamento aprovado |
| `OrcamentoCancelado` | `billing.orcamento-cancelado` | `orcamento_id` (vazio se a OS não tem orçamento) | Ao tratar `CancelarOrcamento`, mesmo repetido |
| `PagamentoReembolsado` | `billing.pagamento-reembolsado` | `pagamento_id`, `valor` (vazio e `"0.00"` se não há pagamento) | Ao tratar `Reembolsar` |
| `ReembolsoFalhou` | `billing.reembolso-falhou` | `pagamento_id`, `detalhe` (`HTTP <código>: <corpo resumido>`) | O provedor recusou o reembolso; o pagamento passa a `REEMBOLSO_FALHOU` |

### Outbox transacional

1. O comando (ou a decisão pela API) abre uma transação do MongoDB.
2. Dentro dela, grava o `message_id` na inbox, muda o estado de orçamento e pagamento e insere cada evento na coleção
   `outbox`, com o contexto do trace atual em `cabecalhos`.
3. Só depois do commit a mensagem do broker recebe `ack`.
4. O relay, na thread de mensageria de cada processo, reivindica as mensagens com
   `find_one_and_update` (`reservada_ate` = agora + 30 s), da mais antiga para a mais nova, até 50 por volta. Publica
   com *publisher confirms* e `mandatory=True` e só então preenche `publicado_em`.
5. Se o broker recusa a mensagem (por exemplo, nenhuma fila ligada à chave porque o OS Service ainda não subiu), o
   relay devolve a reserva e para, sem pular a ordem. A próxima volta tenta de novo.
6. Se o processo cai depois de publicar e antes de marcar, a mensagem sai de novo quando a reserva vence. Quem consome
   absorve a repetição pela inbox.

```mermaid
flowchart LR
  Q["billing-service.comandos"] --> C["tratar_mensagem<br/>valida o envelope"]
  C --> T{"transação<br/>MongoDB"}
  T --> I["inbox<br/>mensagens_processadas"]
  T --> S["estado<br/>orcamentos · pagamentos"]
  T --> O["outbox"]
  T -->|"commit"| A["basic_ack"]
  O --> R["relay<br/>reivindica com reserva de 30 s"]
  R -->|"confirm + mandatory"| E["oficina.eventos"]
  R -->|"confirmado"| M["publicado_em"]
```

### Inbox e idempotência

- O `_id` da `mensagens_processadas` é o `message_id`. Um comando repetido viola a chave única, a transação é encerrada,
  nada muda e a mensagem recebe `ack` (log `mensagem_repetida`).
- Reenvios com outro `message_id` também são inofensivos, porque cada comando é idempotente pelo domínio:
  `GerarOrcamento` pela OS, `Cobrar` pelo orçamento (o `_id` do pagamento é derivado dele), `CancelarOrcamento` e
  `Reembolsar` pelo estado atual.
- Mudanças de estado são condicionais: `replace_one` filtra pelo estado lido e, se ele já mudou, a transação é
  desfeita.

### Ack manual, DLQ e retentativas

| Situação | Resposta do consumidor |
|---|---|
| Mensagem tratada (inclusive repetida) | `basic_ack`, depois do commit |
| Corpo que não é JSON, campos obrigatórios ausentes, `message_id`, `saga_id` ou `os_id` que não são UUID, tipo de comando desconhecido | `basic_nack(requeue=False)`: vai direto para `billing-service.comandos.dlq` (log `mensagem_invalida`) |
| Erro inesperado (por exemplo, o Mercado Pago fora do ar em um `Cobrar`) | `basic_nack(requeue=True)`: a mensagem volta para a fila (log `falha_no_consumo`) e, depois do limite de entregas da quorum queue, segue para a DLQ |

`prefetch_count` é 10. Se o broker ou o banco caem, a thread de mensageria reconecta com espera crescente de 1 s a
30 s (log `mensageria_reconectando`). Sem `RABBITMQ_URL`, a mensageria não sobe: a API atende, mas os eventos ficam
parados no outbox.

### Propagação do trace

O consumo de cada mensagem é uma transação de background do New Relic (`Message/RabbitMQ`, nome igual ao tipo do
comando) que aceita os cabeçalhos de trace da mensagem. Ao gravar um evento no outbox, o Billing guarda os cabeçalhos
do trace corrente; o relay os envia como cabeçalhos AMQP, e o OS Service continua o mesmo trace. No consumo, o
`correlation_id` dos logs passa a ser o `saga_id`.

---

## Papel na Saga

A Saga de abertura da OS é orquestrada pelo OS Service (`app/domain/saga.py` no repositório dele). O Billing é
participante: executa os comandos `GerarOrcamento`, `Cobrar`, `CancelarOrcamento` e `Reembolsar` e responde com
eventos.

### Caminho feliz

```mermaid
sequenceDiagram
    autonumber
    participant OS as OS Service
    participant BI as Billing
    participant CL as Cliente
    participant MP as Mercado Pago

    OS->>BI: GerarOrcamento (billing.gerar-orcamento)
    BI-->>OS: OrcamentoGerado (aviso ao cliente em log)
    CL->>BI: POST /billing/orcamentos/{os_id}/aprovar
    BI-->>OS: OrcamentoAprovado
    OS->>BI: Cobrar (billing.cobrar)
    BI->>MP: POST /v1/orders (type qr, mode dynamic)
    MP-->>BI: id do pedido e qr_data
    BI-->>OS: CobrancaCriada (qr_data, expira_em)
    CL->>MP: paga o QR Code no app do Mercado Pago
    BI->>MP: GET /v1/orders/{id} (conciliação ou webhook)
    MP-->>BI: status processed
    BI-->>OS: PagamentoConfirmado
```

Depois do `PagamentoConfirmado`, o OS Service manda a Execução enfileirar a OS. O Billing não participa dos passos
seguintes, a não ser que uma compensação o chame.

### Falhas e compensações

| Gatilho (decidido pelo OS Service) | O que o Billing recebe e publica |
|---|---|
| Cliente rejeita o orçamento | O Billing publica `OrcamentoRejeitado` (`ORCAMENTO_REJEITADO`). A OS fica `NEGADA` e a reserva é liberada pela Execução |
| Orçamento vence sem decisão | A conciliação publica `OrcamentoRejeitado` (`ORCAMENTO_EXPIRADO`). A OS fica `CANCELADA` |
| Pagamento recusado ou QR vencido | O Billing publica `PagamentoRecusado` (`PAGAMENTO_RECUSADO` ou `PAGAMENTO_EXPIRADO`). O OS Service envia `CancelarOrcamento` e depois `LiberarReserva` |
| Execução recusa o enfileiramento | O OS Service envia `Reembolsar` (Billing responde `PagamentoReembolsado`), depois `CancelarOrcamento` e `LiberarReserva` |
| Prazo de um passo esgotado, ou cancelamento pela oficina | As compensações do ponto em que a Saga parou, na mesma ordem inversa |
| Pagamento chega depois de a Saga decidir compensar ("pagamento tardio") | O `CancelarOrcamento` encontra o pedido pago, e o Billing publica `PagamentoConfirmado` antes de `OrcamentoCancelado`. O OS Service então envia `Reembolsar` |
| `ReembolsoFalhou` | A Saga para em `FALHA_NA_COMPENSACAO` e gera alerta |

O OS Service executa as compensações em ordem inversa, uma por vez: `Reembolsar` (se houve pagamento), depois
`CancelarOrcamento` (se há orçamento ativo), depois `LiberarReserva` (se há reserva). Depois que a OS entra na fila de
execução, a oficina não pode mais cancelar.

Sequência do cancelamento com o pagamento chegando no meio:

```mermaid
sequenceDiagram
    autonumber
    participant OS as OS Service
    participant BI as Billing
    participant MP as Mercado Pago

    Note over OS: a oficina cancela a OS, mas o cliente já pagou
    OS->>BI: CancelarOrcamento
    BI->>MP: POST /v1/orders/{id}/cancel
    MP-->>BI: 409 (pedido já pago)
    BI->>MP: GET /v1/orders/{id}
    MP-->>BI: status processed
    BI-->>OS: PagamentoConfirmado (pago_em)
    BI-->>OS: OrcamentoCancelado
    Note over OS: pagamento tardio: a compensação passa a ser Reembolsar
    OS->>BI: Reembolsar
    BI->>MP: POST /v1/orders/{id}/refund
    MP-->>BI: 2xx
    BI-->>OS: PagamentoReembolsado
```

---

## Mercado Pago

A cobrança passa por uma porta do domínio, `ProvedorPagamento`, com duas implementações escolhidas por
`PAGAMENTO_PROVEDOR`. Domínio e casos de uso são os mesmos nos dois casos, e os testes não dependem de rede.

### Porta ProvedorPagamento

Arquivo `app/domain/ports/provedor_pagamento.py`.

| Método | Contrato |
|---|---|
| `criar_cobranca(pagamento_id, os_id, valor, descricao, expira_em)` | Idempotente pelo `pagamento_id`: repetir a chamada devolve a mesma cobrança (`Cobranca`: `provedor_ref`, `qr_data`, `expira_em`) |
| `consultar(provedor_ref)` | Devolve a `SituacaoPagamento` atual |
| `cancelar(provedor_ref)` | Devolve a situação final; um pedido já pago continua `PAGO` |
| `reembolsar(provedor_ref, chave_idempotencia)` | Levanta `FalhaNoProvedor` quando o provedor recusa |

`SituacaoPagamento`: `PENDENTE`, `PAGO`, `RECUSADO`, `EXPIRADO`, `CANCELADO`, `REEMBOLSADO`.

### Escolha do adapter

| `PAGAMENTO_PROVEDOR` | Adapter | Quando usar |
|---|---|---|
| `simulado` (padrão) | `ProvedorSimulado` (`app/adapters/outbound/pagamento/simulado.py`) | Desenvolvimento local, testes, e quem reproduz o projeto sem conta no Mercado Pago |
| `mercadopago` (sem distinguir maiúsculas) | `ProvedorMercadoPago` (`app/adapters/outbound/pagamento/mercadopago.py`) | Cobrança real no sandbox ou em produção do Mercado Pago |

Qualquer outro valor cai no simulador. Com `mercadopago`, `MP_ACCESS_TOKEN` e `MP_EXTERNAL_POS_ID` são obrigatórios: sem
eles a subida falha com `PAGAMENTO_PROVEDOR=mercadopago exige MP_ACCESS_TOKEN e MP_EXTERNAL_POS_ID`. No cluster, o ConfigMap
traz `PAGAMENTO_PROVEDOR: "simulado"` e o pipeline o troca para `mercadopago` quando o secret `MP_ACCESS_TOKEN` existe.

### Adapter simulado

- A situação de cada cobrança fica na coleção `simulador_pagamentos`; o `provedor_ref` é o próprio id do pagamento e o
  `qr_data` é fictício (`00020101SIMULADO<id>`).
- Uma cobrança nasce `PENDENTE`. Repetir `criar_cobranca` não reinicia a situação.
- `cancelar` só cancela uma cobrança `PENDENTE`; `reembolsar` marca como `REEMBOLSADO`.
- Quem muda a situação é a rota `POST /billing/simulador/pagamentos/{pagamento_id}/{acao}`, restrita a funcionário, com
  `acao` igual a `aprovar` (`PAGO`), `recusar` (`RECUSADO`) ou `expirar` (`EXPIRADO`). A rota responde 204, e a
  conciliação periódica publica o resultado, como faria com o provedor real. Com o Mercado Pago ativo, a rota responde 404
  (`Simulador desligado`).

### Adapter Mercado Pago

Cliente HTTP `httpx`, tempo limite de 10 s, `Authorization: Bearer <MP_ACCESS_TOKEN>`, base em `MP_API_URL` (padrão
`https://api.mercadopago.com`).

| Operação | Chamada | Cabeçalho de idempotência | Observações |
|---|---|---|---|
| Criar cobrança | `POST /v1/orders` | `X-Idempotency-Key` = id do pagamento | Corpo abaixo. O `qr_data` vem de `type_response.qr_data` (ou `qr_data` na raiz) e o `provedor_ref` é o `id` do pedido |
| Consultar | `GET /v1/orders/{id}` | n/a | Traduz o `status` do pedido (tabela abaixo) |
| Cancelar | `POST /v1/orders/{id}/cancel` | `X-Idempotency-Key` = `cancelar-<id do pedido>` | Responde 200 com o status final. `409` (pedido pago, vencido ou já cancelado) é resolvido reconsultando o pedido |
| Reembolsar | `POST /v1/orders/{id}/refund` | `X-Idempotency-Key` = `reembolso:<id do pagamento>` | Reembolso total, só de pedido `processed`. Resposta `>= 400` vira `FalhaNoProvedor` com `HTTP <código>: <corpo, até 200 caracteres>` |

Corpo de `POST /v1/orders`:

```json
{
  "type": "qr",
  "external_reference": "<os_id>",
  "description": "Ordem de serviço <os_id>",
  "total_amount": "150.00",
  "expiration_time": "PT15M",
  "config": { "qr": { "external_pos_id": "<MP_EXTERNAL_POS_ID>", "mode": "dynamic" } },
  "transactions": { "payments": [ { "amount": "150.00" } ] },
  "items": [ { "title": "Ordem de serviço <os_id>", "unit_price": "150.00", "quantity": 1, "unit_measure": "unit" } ]
}
```

- `description` e `title` são cortados em 150 caracteres. O item existe para que o app do Mercado Pago mostre o
  nome da OS ao comprador, e não "default item".
- `expiration_time` é `PT<minutos>M`, com os minutos restantes até `expira_em` arredondados para cima (mínimo 1).
  `expira_em` é o momento da cobrança mais `PAGAMENTO_EXPIRACAO_MINUTOS` (padrão 15).
- O corpo é determinístico para um mesmo pagamento: o Mercado Pago responde 409 quando a mesma
  `X-Idempotency-Key` volta com corpo diferente.
- O `external_reference` é o id da OS.

Tradução do `status` do pedido para `SituacaoPagamento`:

| `status` do pedido | Situação |
|---|---|
| `processed` | `PAGO` |
| `canceled` | `CANCELADO` |
| `expired` | `EXPIRADO` |
| `refunded` | `REEMBOLSADO` |
| `failed` | `RECUSADO` |
| outro, com algum pagamento da transação em `rejected` ou `failed` | `RECUSADO` |
| qualquer outro (por exemplo `created`, `action_required`) | `PENDENTE` |

Comportamentos conferidos no sandbox do Mercado Pago e registrados no código: o cancelamento exige `X-Idempotency-Key`
(sem ela o Mercado Pago responde 400), responde 200 e dá 409 fora do status `created`; o reembolso só aceita pedido
`processed`.

### Conciliação periódica

`ConciliarPagamentosUseCase` (`app/application/use_cases/conciliar_pagamentos.py`) roda na thread de mensageria de cada
processo, a cada `CONCILIACAO_SEGUNDOS` (padrão 15). Ela funciona sem URL pública, por isso é o caminho principal de
confirmação; o webhook só acelera. Cada volta faz duas varreduras, em lotes de 50 ordenados por `_id`:

1. **Orçamentos vencidos.** Todo orçamento `GERADO` com `expira_em` no passado passa a `EXPIRADO` e publica
   `OrcamentoRejeitado` com motivo `ORCAMENTO_EXPIRADO`, em uma transação por orçamento.
2. **Pagamentos pendentes.** Para cada pagamento `PENDENTE`, consulta o provedor e aplica o resultado:

| Situação no provedor | Efeito no pagamento | Evento |
|---|---|---|
| `PAGO` | `CONFIRMADO`, com `pago_em` | `PagamentoConfirmado` |
| `RECUSADO` | `RECUSADO` | `PagamentoRecusado` (`PAGAMENTO_RECUSADO`) |
| `EXPIRADO` | `EXPIRADO` | `PagamentoRecusado` (`PAGAMENTO_EXPIRADO`) |
| `CANCELADO` | `CANCELADO` | nenhum |
| `PENDENTE`, dentro do prazo | nenhum | nenhum |
| `REEMBOLSADO` | nenhum (reembolso feito direto no provedor não é tratado aqui) | nenhum |

**Cancelamento de garantia no fim do prazo.** Quando o pedido está `EXPIRADO` no provedor, ou ainda `PENDENTE` com o
`expira_em` do pagamento já vencido, o Billing chama `cancelar` antes de decidir, porque o QR pode ter sido pago no
instante em que expirou. Se o cancelamento devolve `PAGO`, o pagamento é confirmado. Caso contrário, é expirado. Assim
o QR Code não fica pagável depois de a Saga ter começado a compensar.

Falhas são isoladas por item: o erro de um pagamento ou orçamento é registrado (`conciliacao_falhou`) e não impede os
demais. O item volta na próxima volta. Duas réplicas que conciliam o mesmo pagamento ao mesmo tempo disputam a
atualização condicional: só uma publica o evento.

### Webhook

`POST /billing/webhooks/mercadopago`, sem JWT. A autenticidade vem da assinatura `x-signature`.

1. O cabeçalho `x-signature` tem a forma `ts=<timestamp>,v1=<hmac>`.
2. O manifesto é a concatenação, nesta ordem e só com as partes presentes, de `id:<data.id>;` (em minúsculas quando
   `data.id` é alfanumérico), `request-id:<x-request-id>;` e `ts:<ts>;`. O `data.id` vem da query `data.id`.
3. O esperado é `HMAC-SHA256(MP_WEBHOOK_SECRET, manifesto)` em hexadecimal, comparado com `v1` em tempo constante.
4. Assinatura inválida, ou `MP_WEBHOOK_SECRET` não configurado, responde 401 (log `webhook_assinatura_invalida`).
5. Corpo com `type` diferente de `order` responde 200 com `{"processado": false}`.
6. Notificação válida: o Billing procura o pagamento por `provedor_ref` e **sempre reconsulta o pedido** no Mercado Pago.
   O corpo da notificação nunca decide o estado. Pedido desconhecido responde `{"processado": false}`
   (log `notificacao_sem_pagamento`).

O webhook só chega ao Billing quando o Mercado Pago consegue alcançar a URL. Cada ambiente novo exige cadastrar no painel
do Mercado Pago a URL `<endpoint do API Gateway>/billing/webhooks/mercadopago` para o tópico de pedidos (`order`) e
copiar a chave secreta de assinatura para o secret `MP_WEBHOOK_SECRET`. O endpoint do gateway está no parâmetro SSM
`/tech-challenge-f4/apigateway/endpoint`:

```bash
aws ssm get-parameter --name /tech-challenge-f4/apigateway/endpoint --query Parameter.Value --output text
```

Sem URL cadastrada (ou sem `MP_WEBHOOK_SECRET`), a conciliação periódica confirma o pagamento do mesmo jeito.

### Preparar a loja e o caixa no Mercado Pago

O QR Code dinâmico exige uma loja e um caixa (POS) na conta do Mercado Pago. O script `scripts/mercadopago_preparar.py`
os cria uma única vez e imprime o `external_pos_id`, valor da variável `MP_EXTERNAL_POS_ID`.

Pré-requisitos: uma aplicação no painel de desenvolvedores do Mercado Pago com credenciais de teste, o access token
dessa conta vendedora (`MP_ACCESS_TOKEN`) e o id numérico do usuário dono do token (`MP_USER_ID`, exigido pelas rotas
`/users/{user_id}/stores`).

```bash
make venv
MP_ACCESS_TOKEN="<access token de teste>" MP_USER_ID="<id do usuário vendedor>" \
  .venv/bin/python scripts/mercadopago_preparar.py            # caixa padrão: oficinacaixa1
# ou, com outro identificador (só letras e números):
MP_ACCESS_TOKEN="..." MP_USER_ID="..." .venv/bin/python scripts/mercadopago_preparar.py meucaixa
```

O script:

1. busca a loja `oficina-loja-1` (`GET /users/{user_id}/stores/search?external_id=oficina-loja-1`) e, se não existe,
   cria (`POST /users/{user_id}/stores`, nome "Oficina");
2. cria o caixa "Caixa 1" (`POST /v2/pos`, com `X-Idempotency-Key` aleatório, `store_id` como texto e o `external_id`
   informado);
3. tolera `pos_already_exists`, então pode ser executado de novo;
4. imprime o `external_id` do caixa, que é o `MP_EXTERNAL_POS_ID`.

O `external_id` do caixa aceita apenas letras e números. `MP_API_URL` também é lido do ambiente.

### Testar com a conta compradora de teste

1. Prepare a loja e o caixa (seção anterior) e anote o `MP_EXTERNAL_POS_ID`.
2. Configure o Billing com o provedor real. Localmente, no `.env`:
   ```dotenv
   PAGAMENTO_PROVEDOR=mercadopago
   MP_ACCESS_TOKEN=<access token de teste>
   MP_EXTERNAL_POS_ID=<external_id do caixa>
   ```
   Sem URL pública, deixe `MP_WEBHOOK_SECRET` vazio: a conciliação periódica confirma o pagamento. Na nuvem, grave os três
   valores como secrets do repositório (veja [Pipeline CI/CD](#pipeline-cicd)).
3. Leve uma OS até a cobrança: no ambiente integrado, pela Saga (abrir a OS e aprovar o orçamento); isoladamente, pelo
   script da seção [Exercitar o Billing sozinho](#exercitar-o-billing-sozinho).
4. Leia o QR Code: `GET /billing/orcamentos/{os_id}` devolve `pagamento.qr_data`, o conteúdo do QR Code dinâmico
   (formato Pix copia e cola). Gere uma imagem QR a partir dessa string, com qualquer gerador de QR Code.
5. No celular, abra o app do Mercado Pago **logado na conta compradora de teste** e leia o QR. O resultado (aprovado ou
   recusado) depende do meio de pagamento de teste usado nessa conta.
6. Em até `CONCILIACAO_SEGUNDOS`, o Billing consulta o pedido e publica `PagamentoConfirmado`. O log `pagamento_conciliado`
   mostra o estado final, e o pagamento passa a `CONFIRMADO`.
7. Para exercitar o reembolso, cancele a OS depois do pagamento, ou envie `Reembolsar`: o Billing chama
   `POST /v1/orders/{id}/refund` e o pedido fica `refunded` no Mercado Pago.

Comportamento validado em nuvem (AWS Academy, `us-east-1`, 3 nós `t3.medium`) com o Mercado Pago de teste:

- fluxo completo, com pagamento real pelo app do Mercado Pago (conta compradora de teste), até a OS `ENTREGUE`;
- QR vencido, com a Saga compensando;
- cancelamento da OS com o pagamento chegando no meio, terminando em reembolso (`refunded` no Mercado Pago);
- mensagens aguardando no RabbitMQ enquanto o Billing esteve com 0 réplicas.

---

## API REST

Todas as rotas de negócio têm o prefixo `/billing`. O JWT vai em `Authorization: Bearer <token>`; o serviço só confere a
assinatura (`SECRET_KEY`, `ALGORITHM`) e os claims, sem banco de usuários:

- **funcionário**: `tipo=usuario` e `role` igual a `ADMIN` ou `ATENDENTE` (token do `POST /auth/token` do OS Service);
- **cliente**: `tipo=cliente`, com `sub` igual ao id do cliente (token da Lambda de autenticação, `POST /auth/cpf`).

| Método | Caminho | Perfil exigido | Descrição |
|---|---|---|---|
| `GET` | `/billing/orcamentos/{os_id}` | Funcionário (qualquer orçamento) ou cliente titular | Orçamento da OS e o pagamento, se houver. Cliente de outro titular recebe 404 |
| `POST` | `/billing/orcamentos/{os_id}/aprovar` | Cliente titular ou funcionário | Aprova o orçamento e publica `OrcamentoAprovado`. Orçamento vencido ou já decidido responde 422; cliente de outro titular, 404 |
| `POST` | `/billing/orcamentos/{os_id}/rejeitar` | Cliente titular ou funcionário | Rejeita o orçamento e publica `OrcamentoRejeitado`. Orçamento já decidido responde 422; cliente de outro titular, 404 |
| `POST` | `/billing/simulador/pagamentos/{pagamento_id}/{acao}` | Só funcionário | Com o provedor simulado, `acao` em `aprovar`, `recusar` ou `expirar`. Responde 204; 404 com o Mercado Pago ativo ou pagamento desconhecido; 403 para token de cliente |
| `POST` | `/billing/webhooks/mercadopago` | Público (assinatura `x-signature`) | Notificação do Mercado Pago. Veja [Webhook](#webhook) |
| `GET` | `/health` | Público | Liveness, sem acesso ao banco |
| `GET` | `/health/ready` | Público | Readiness: confirma o MongoDB (503 se estiver inacessível) |

`os_id` é um UUID; um valor inválido responde 422.

Erros devolvem `{"detail": "...", "correlation_id": "..."}` com os códigos: 401 (token ausente, inválido ou expirado),
403 (token de cliente em rota de funcionário), 404 (não encontrado), 409 (`O estado mudou antes da gravação`, quando
outra operação decidiu antes) e 422 (regra de negócio violada).

Exemplo de `GET /billing/orcamentos/{os_id}` (valores ilustrativos):

```json
{
  "id": "7d433432-9343-4a41-806e-55957924b6b0",
  "os_id": "66fb4f6f-a4e9-4ce7-a246-2c46ff9b4af3",
  "estado": "APROVADO",
  "cliente": { "id": "...", "nome": "Ana Souza", "email": "ana@example.com", "telefone": null },
  "itens": [ { "tipo": "SERVICO", "item_id": "...", "descricao": "Troca de óleo", "quantidade": 1,
               "preco_unitario": "150.00", "subtotal": "150.00" } ],
  "valor_total": "150.00",
  "expira_em": "2025-01-16T12:00:00Z",
  "aprovado_por": "CLIENTE",
  "criado_em": "2025-01-15T12:00:00Z",
  "pagamento": {
    "id": "495e90f0-42b9-5d27-95c5-ace7d46d2e4e",
    "provedor": "SIMULADO",
    "estado": "CONFIRMADO",
    "valor": "150.00",
    "qr_data": "00020101SIMULADO495e90f0-42b9-5d27-95c5-ace7d46d2e4e",
    "expira_em": "2025-01-15T12:15:00Z",
    "pago_em": "2025-01-15T12:03:00Z"
  }
}
```

A resposta de aprovar e rejeitar tem o mesmo formato (o campo `pagamento` vem vazio).

### Swagger

| Onde | URL |
|---|---|
| Local (docker compose) | http://localhost:8003/docs · `/redoc` · `/openapi.json` |
| Nuvem, pelo API Gateway | `<endpoint do API Gateway>/billing/docs` · `/billing/redoc` · `/billing/openapi.json` |

O gateway não reescreve caminhos, então o serviço publica o Swagger sob o prefixo configurado em `PREFIXO_DOCS`
(`/billing` no cluster, vazio localmente). Assim ele não colide com o Swagger do OS Service, que fica em `/docs`.

### Coleção Postman

A coleção `oficina-fase04.postman_collection.json` e o ambiente local ficam em
[`docs/postman/`](https://github.com/pesant0s/tech-challenge-f4-plataforma/tree/main/docs/postman) na plataforma. As
pastas "Caminho feliz", "Compensação - orçamento rejeitado", "Compensação - cancelamento de OS já paga" e "Regras de
acesso" chamam as rotas do Billing (variável `billingUrl`, `http://localhost:8003` no ambiente local) e usam o simulador
de pagamento.

---

## Configuração

Lidas por `app/infrastructure/config.py` (Pydantic Settings), do ambiente ou do arquivo `.env`.

| Variável | Padrão | Onde é definida no cluster | Descrição |
|---|---|---|---|
| `MONGO_URL` | obrigatória | Secret | URL do MongoDB. Localmente `mongodb://mongo:27017/?directConnection=true`; no cluster `mongodb://mongo.billing-service.svc.cluster.local:27017/?directConnection=true` |
| `MONGO_DB` | `billing` | ConfigMap | Nome do banco |
| `SECRET_KEY` | obrigatória | Secret (SSM `segredos/jwt_secret_key`) | Chave que assina os JWT. Deve ser a mesma do OS Service |
| `ALGORITHM` | `HS256` | ConfigMap | Algoritmo do JWT |
| `ALLOWED_ORIGINS` | `["http://localhost:3000"]` | ConfigMap (`["*"]`) | Origens do CORS, em formato de lista JSON. O CORS não permite credenciais |
| `LOG_FORMAT` | `console` | ConfigMap (`json`) | `json` ou `console` |
| `LOG_LEVEL` | `INFO` | ConfigMap | Nível dos logs |
| `PREFIXO_DOCS` | vazio | ConfigMap (`/billing`) | Prefixo de `/docs`, `/redoc` e `/openapi.json` |
| `RABBITMQ_URL` | sem valor | Secret (SSM `rabbitmq/url`) | URL AMQP. Sem ela, a API atende, mas não consome comandos, não publica eventos e não concilia pagamentos |
| `PAGAMENTO_PROVEDOR` | `simulado` | ConfigMap (`simulado`, trocado pelo pipeline) | `simulado` ou `mercadopago` |
| `MP_API_URL` | `https://api.mercadopago.com` | n/a | URL base da API do Mercado Pago |
| `MP_ACCESS_TOKEN` | sem valor | Secret (secret do GitHub) | Access token do Mercado Pago. Obrigatório com `mercadopago` |
| `MP_EXTERNAL_POS_ID` | sem valor | Secret (secret do GitHub) | `external_id` do caixa. Obrigatório com `mercadopago` |
| `MP_WEBHOOK_SECRET` | sem valor | Secret (secret do GitHub) | Chave de assinatura do webhook. Sem ela, o webhook responde 401 |
| `PAGAMENTO_EXPIRACAO_MINUTOS` | `15` | n/a | Validade do QR Code, a partir da criação da cobrança |
| `ORCAMENTO_VALIDADE_MINUTOS` | `1440` | n/a | Validade do orçamento, a partir da geração |
| `CONCILIACAO_SEGUNDOS` | `15` | n/a | Intervalo da conciliação periódica |
| `NEW_RELIC_LICENSE_KEY` | vazio (agente desligado) | Secret (SSM `newrelic/license_key`, só se tiver valor real) | Licença do New Relic |
| `NEW_RELIC_APP_NAME` | `tech-challenge-f4-billing-service` no `.env.example` | ConfigMap | Nome da aplicação no New Relic |
| `NEW_RELIC_APPLICATION_LOGGING_FORWARDING_ENABLED` | n/a | ConfigMap (`false`) | O stdout já chega pelo `nri-bundle`; o encaminhamento do agente duplicaria cada linha |
| `UVICORN_WORKERS` | `2` | n/a | Processos do Uvicorn (lida pelo `entrypoint.sh`) |

Somente nos testes: `MONGO_URL_TESTE` (padrão `mongodb://localhost:27017/?replicaSet=rs0`), `RABBITMQ_URL_TESTE` (padrão
`amqp://oficina:oficina@localhost:5672/%2F`) e `CONTRATO_ASYNCAPI` (caminho do `asyncapi.yaml`).

O `.env.example` traz todos os valores para o docker compose; `make setup` o copia para `.env`, sem sobrescrever um
`.env` existente. O `.env` não é versionado.

---

## Como rodar localmente

Requisitos: Docker com Compose v2, `make` e, para rodar os testes fora de contêiner, Python 3.12.

### Alvos do Makefile

`make help` lista os alvos.

| Alvo | O que faz |
|---|---|
| `make setup` | Cria o `.env` a partir do `.env.example`, sem sobrescrever |
| `make up` | Sobe API, MongoDB e RabbitMQ com hot-reload (http://localhost:8003/docs) |
| `make down` | Derruba a stack local e remove os volumes |
| `make logs` | Acompanha os logs da API |
| `make sh` | Abre um shell no contêiner da API |
| `make venv` | Cria o ambiente virtual `.venv` (testado em Python 3.12) e instala `requirements.txt` |
| `make test` | Roda os testes (exige o MongoDB em replica set em `localhost:27017`) |
| `make test-docker` | Roda os testes dentro do contêiner, sem Python na máquina |
| `make cov` | Testes com cobertura (`--cov=app`, relatório no terminal e `coverage.xml`) |
| `make build` | Constrói a imagem local `tech-challenge-f4-billing-service:<sha curto>` |
| `make render` | Renderiza os manifestos do cluster (`kubectl kustomize k8s/overlays/producao`) |
| `make implantar` | Aciona o pipeline de deploy na `main` (`gh workflow run`) |
| `make conta` | Mostra em qual conta AWS os comandos de infraestrutura atuam |
| `make infra-init` | Inicializa o Terraform com o estado desta conta |
| `make infra-up` | Cria o ECR (a plataforma precisa estar no ar) |
| `make infra-down` | Destrói o ECR |
| `make infra-validate` | Valida o Terraform, sem AWS (`fmt -check`, `init -backend=false`, `validate`) |

### Billing isolado, com docker compose

```bash
make up                 # cria o .env se faltar e sobe api, mongo e rabbitmq
```

| Serviço do compose | Imagem | Porta no host |
|---|---|---|
| `api` | build local, com o código montado e `uvicorn --reload` | 8003 → 8000 |
| `mongo` | `mongo:8.0`, `--replSet rs0`, o health check inicia o replica set | 27017 |
| `rabbitmq` | `rabbitmq:4.1-alpine`, usuário `oficina`, senha `oficina` | 5672 |

A API espera o MongoDB e o RabbitMQ ficarem saudáveis (`depends_on` com `service_healthy`). O Swagger fica em
http://localhost:8003/docs. Para encerrar: `make down` (remove também os volumes).

### Exercitar o Billing sozinho

Sem OS Service nem Execução, dá para conduzir um orçamento do começo ao fim: o script abaixo faz o papel do OS
Service (publica `GerarOrcamento`, `Cobrar` e `Reembolsar`), do cliente (aprova o orçamento) e do funcionário (aprova o
pagamento no simulador), e acompanha os eventos publicados. Ele gera os JWT com a mesma `SECRET_KEY` do serviço.
Com `make up` em execução (em outro terminal, ou com `docker compose up -d --build`):

```bash
docker compose exec -T api python - <<'EOF'
import json, os, time, uuid
from datetime import datetime, timedelta, timezone
import httpx, pika
from jose import jwt

BASE = os.environ.get("BASE", "http://localhost:8000")   # dentro do contêiner a API escuta na 8000
SECRET = os.environ["SECRET_KEY"]

def token(**claims):
    claims["exp"] = datetime.now(timezone.utc) + timedelta(hours=1)
    return {"Authorization": "Bearer " + jwt.encode(claims, SECRET, algorithm="HS256")}

cliente_id = str(uuid.uuid4())
funcionario = token(sub="atendente", tipo="usuario", role="ATENDENTE")
cliente = token(sub=cliente_id, tipo="cliente")

canal = pika.BlockingConnection(pika.URLParameters(os.environ["RABBITMQ_URL"])).channel()
fila = canal.queue_declare("", exclusive=True).method.queue
canal.queue_bind(fila, "oficina.eventos", "billing.#")          # escuta os eventos do Billing

os_id, saga_id = str(uuid.uuid4()), str(uuid.uuid4())

def comando(tipo, rota, payload):
    corpo = {"message_id": str(uuid.uuid4()), "tipo": tipo, "versao": 1, "saga_id": saga_id, "os_id": os_id,
             "ocorrido_em": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
             "origem": "os-service", "payload": payload}
    canal.basic_publish("oficina.comandos", rota, json.dumps(corpo).encode(),
                        pika.BasicProperties(content_type="application/json", delivery_mode=2))

def aguardar(tipo):
    for _ in range(300):
        metodo, _, corpo = canal.basic_get(fila, auto_ack=True)
        if metodo:
            evento = json.loads(corpo)
            print("evento:", evento["tipo"], json.dumps(evento["payload"]))
            if evento["tipo"] == tipo:
                return evento
        else:
            time.sleep(0.1)
    raise SystemExit(f"{tipo} não chegou")

comando("GerarOrcamento", "billing.gerar-orcamento", {
    "cliente": {"id": cliente_id, "nome": "Ana Souza", "email": "ana@example.com", "telefone": None},
    "itens": [{"tipo": "SERVICO", "item_id": str(uuid.uuid4()), "descricao": "Troca de óleo", "quantidade": 1,
               "preco_unitario": "150.00", "subtotal": "150.00"}],
    "valor_total": "150.00"})
orcamento_id = aguardar("OrcamentoGerado")["payload"]["orcamento_id"]

print("aprovar:", httpx.post(f"{BASE}/billing/orcamentos/{os_id}/aprovar", headers=cliente).status_code)
aguardar("OrcamentoAprovado")

comando("Cobrar", "billing.cobrar", {"orcamento_id": orcamento_id})
pagamento_id = aguardar("CobrancaCriada")["payload"]["pagamento_id"]

print("simulador:", httpx.post(f"{BASE}/billing/simulador/pagamentos/{pagamento_id}/aprovar", headers=funcionario).status_code)
aguardar("PagamentoConfirmado")          # a conciliação publica em até CONCILIACAO_SEGUNDOS

comando("Reembolsar", "billing.reembolsar", {"motivo": "CANCELADA_PELA_OFICINA"})
aguardar("PagamentoReembolsado")
print(httpx.get(f"{BASE}/billing/orcamentos/{os_id}", headers=funcionario).json()["pagamento"]["estado"])
EOF
```

A sequência de eventos esperada é `OrcamentoGerado`, `OrcamentoAprovado`, `CobrancaCriada`, `PagamentoConfirmado` e
`PagamentoReembolsado`, e o estado final do pagamento é `REEMBOLSADO`. Para testar a recusa, troque `aprovar` por `recusar`
na chamada do simulador e espere `PagamentoRecusado`; com `expirar`, o motivo é `PAGAMENTO_EXPIRADO`. As mesmas
operações podem ser feitas pelo Swagger, com um token gerado como acima.

### Rodar os testes

```bash
make setup                                   # o compose lê o .env
docker compose up -d --wait mongo rabbitmq   # MongoDB em replica set e RabbitMQ locais
make test                                    # pytest contra localhost:27017
make cov                                     # com cobertura
make test-docker                             # alternativa: tudo dentro do contêiner
```

Os testes criam um banco com nome aleatório por teste e o apagam no fim. Dois testes usam um RabbitMQ real e são pulados
quando `localhost:5672` não responde. Detalhes em [Testes e cobertura](#testes-e-cobertura).

### Ambiente integrado (os três serviços)

Para a Saga completa na máquina, sem nuvem, use o ambiente local da plataforma, que sobe o RabbitMQ e os bancos dos três
serviços:

```bash
cd ../tech-challenge-f4-plataforma
make local-up           # RabbitMQ (5672), PostgreSQL do OS e da Execução, MongoDB rs0 (27017)
```

O `make local-up` e o compose deste repositório publicam as mesmas portas (27017 e 5672): use um ou o outro, não os dois.
Com o ambiente da plataforma ligado, rode o Billing fora do compose, apontando para ele:

```bash
make venv
MONGO_URL="mongodb://localhost:27017/?replicaSet=rs0" \
RABBITMQ_URL="amqp://oficina:oficina@localhost:5672/%2F" \
SECRET_KEY="<a mesma SECRET_KEY do OS Service>" \
.venv/bin/python -m uvicorn app.main:app --port 8003
```

Suba o OS Service e a Execução conforme os READMEs deles e use a coleção Postman da plataforma (o ambiente local espera o
Billing em `http://localhost:8003`).

---

## Testes e cobertura

Suíte `pytest` com 187 testes, em `tests/`.

| Arquivo | Testes | O que cobre |
|---|---|---|
| `test_dominio.py` | 33 | Estados e transições de orçamento e pagamento, id estável do pagamento, formato do dinheiro |
| `test_comandos_saga.py` | 23 | `GerarOrcamento`, `Cobrar`, `CancelarOrcamento` e `Reembolsar`: efeitos, idempotência, mensagens repetidas, falha de reembolso, atomicidade da transação |
| `test_conciliacao.py` | 17 | Conciliação: pago, recusado, expirado, cancelamento de garantia, orçamento vencido, disputa entre réplicas, lotes, notificação do provedor |
| `test_mensageria.py` | 27 | Consumo com ack e nack, DLQ, relay do outbox (ordem, limite, reserva, broker recusando), topologia, rastreamento e, com RabbitMQ real, a thread de mensageria |
| `test_mercadopago.py` | 31 | Adapter do Mercado Pago contra respostas simuladas (corpo e cabeçalhos), tradução de status, webhook e assinatura `x-signature` |
| `test_simulador.py` | 13 | Provedor simulado e a rota do simulador |
| `test_rotas.py` | 20 | Consulta, aprovação e rejeição pela API, titularidade, JWT |
| `test_observabilidade.py` | 23 | Logs JSON e console, correlação, cabeçalhos de segurança, health, composição e ciclo de vida, `PREFIXO_DOCS` |

Como os testes são montados:

- usam um **MongoDB real em replica set** (`localhost:27017`), porque o serviço depende de transações. Sem ele, a execução
  termina com a mensagem de como subir o banco;
- o Mercado Pago é simulado com `respx` (sem rede); o provedor simulado cobre o restante do fluxo;
- toda mensagem que o Billing publica é validada contra o JSON Schema do `asyncapi.yaml` da plataforma
  (`jsonschema`) e a chave de roteamento é conferida contra o canal do contrato. O arquivo é procurado em
  `../tech-challenge-f4-plataforma/docs/contratos/asyncapi.yaml` (ou em `CONTRATO_ASYNCAPI`); com os repositórios
  lado a lado, a conferência roda. No CI, que clona só este repositório, ela é pulada;
- dois testes (`test_trabalhador_contra_rabbitmq_real` e `test_trabalhador_concilia_o_pagamento_periodicamente`) usam um RabbitMQ
  real e são pulados se `localhost:5672` não responde. O CI sobe um RabbitMQ como service container.

**Resultado da suíte completa** (MongoDB em replica set e RabbitMQ locais, repositórios lado a lado): 187 testes passam
e a cobertura de `app/` é de 100% (955 instruções). A contagem vem de `python -m pytest --collect-only -q`.

**Cobertura no CI.** O job de testes roda `pytest --cov=app --cov-report=term-missing --cov-report=xml --cov-fail-under=80 -q`:
abaixo de 80% o pipeline falha.

**Análise do SonarQube Cloud** (projeto `pesant0s_tech-challenge-f4-billing-service`), lida na API pública:

```bash
curl -s "https://sonarcloud.io/api/measures/component?component=pesant0s_tech-challenge-f4-billing-service&metricKeys=coverage,bugs,vulnerabilities,code_smells"
```

| Métrica | Valor |
|---|---|
| Quality gate | aprovado (`OK`) |
| Cobertura | 97,2% |
| Bugs | 0 |
| Vulnerabilidades | 4 |
| Code smells | 1 |

O Sonar mede o repositório inteiro (o `sonar-project.properties` exclui só `tests/` da cobertura), por isso o valor
difere dos 100% de `app/` medidos localmente; o script `scripts/mercadopago_preparar.py` não tem teste. Os valores mudam
a cada análise. As quatro vulnerabilidades apontadas estão em arquivos de pipeline e de imagem: o `pip install -r
requirements.txt` do workflow (sem `--only-binary` e sem versões travadas por hash), o `curl` que baixa o kubeconform e o
`COPY . .` do `Dockerfile`. O code smell é a posição do `CORSMiddleware` na cadeia de middlewares de `app/main.py`.

---

## Pipeline CI/CD

Arquivo `.github/workflows/ci-cd.yml`.

**Gatilhos:** `push` em `develop` e `main`, `pull_request` para `develop` e `main`, e `workflow_dispatch` (usado por
`make implantar`). O grupo de concorrência cancela execuções anteriores apenas em pull request.

```mermaid
flowchart LR
  T["testes<br/>pytest + cobertura >= 80%"] --> Q["qualidade<br/>SonarQube Cloud"]
  T --> I["imagem<br/>build e prova"]
  M["manifestos<br/>kustomize + kubeconform"] --> I
  Q --> D{"main e<br/>AMBIENTE_ATIVO"}
  M --> D
  I --> D
  T --> D
  D -->|"true"| DEP["deploy no EKS"]
  D -->|"diferente de true"| PUL["ambiente-desligado<br/>publicação e deploy pulados"]
```

| Job | Quando roda | O que faz |
|---|---|---|
| `testes` (Testes e cobertura) | Sempre | MongoDB 8.0 em replica set (`docker run`), RabbitMQ 4.1 como service container, Python 3.12, `pip install`, `pytest --cov=app --cov-report=xml --cov-fail-under=80`; publica o `coverage.xml` como artefato |
| `qualidade` (Qualidade, SonarQube Cloud) | Depois de `testes` | Baixa o `coverage.xml` e roda o scan; o quality gate reprova o pipeline (`sonar.qualitygate.wait=true`) |
| `manifestos` (Validar manifestos Kubernetes) | Sempre | Renderiza `k8s/overlays/producao` e valida com `kubeconform` 0.6.7 em modo estrito |
| `imagem` (Build e prova da imagem) | Depois de `testes` e `manifestos` | Constrói a imagem, sobe MongoDB e RabbitMQ e prova que o contêiner fica pronto (`/health/ready` responde e o log traz `mensageria_conectada`) |
| `ambiente-desligado` | `main`, sem ser PR, com `AMBIENTE_ATIVO` diferente de `true` | Registra no resumo que publicação e deploy foram pulados |
| `deploy` (Deploy no EKS) | `main`, sem ser PR, com `AMBIENTE_ATIVO` igual a `true`, depois de `testes`, `qualidade`, `manifestos` e `imagem` | Veja abaixo |

**Proteção da `main`.** A `main` só recebe código por pull request. Checks obrigatórios: `Testes e cobertura`,
`Validar manifestos Kubernetes`, `Build e prova da imagem` e `Qualidade (SonarQube Cloud)`. A regra vale também para
administradores, e não há push forçado.

**`AMBIENTE_ATIVO`.** Variável do repositório (`true` ou não). Com o ambiente AWS desligado, os testes, a qualidade, a
validação dos manifestos e a prova da imagem continuam obrigatórios, mas a publicação e o deploy são pulados. A plataforma
liga e desliga a variável (`make up`, `make down`, `make ambiente-ligar`, `make ambiente-desligar`).

**Passos do deploy no EKS** (job `deploy`):

1. Autentica na AWS (abaixo).
2. Instala o Terraform e cria o ECR (`terraform -chdir=infra init` com o bucket de estado da conta e `apply -auto-approve`).
3. Faz login no ECR.
4. Constrói a imagem e a publica com a tag dos 7 primeiros caracteres do commit.
5. Lê o nome do cluster no SSM (`/tech-challenge-f4/cluster/nome`) e aponta o `kubectl` para ele.
6. Cria o namespace `billing-service`.
7. Monta o Secret `billing-service-segredos` (ver abaixo).
8. Aplica os manifestos: se o secret `MP_ACCESS_TOKEN` existe, troca `PAGAMENTO_PROVEDOR` para `mercadopago` no ConfigMap
   da cópia de trabalho; define a imagem no overlay (`kustomize edit set image`); `kubectl apply -k
   k8s/overlays/producao`; `kubectl rollout restart` do Deployment (Secret e ConfigMap entram por variável de ambiente,
   e só um pod novo os enxerga); espera o `rollout status` do StatefulSet `mongo` e do Deployment.
9. Teste de fumaça: `GET <endpoint do API Gateway>/billing/openapi.json`, com até 12 tentativas a cada 10 s (o health
   check do NLB leva alguns ciclos para marcar os pods como saudáveis).
10. Em caso de falha, imprime `kubectl get all`, os logs do Billing e os do MongoDB.

**Autenticação na AWS.** O job escolhe sozinho:

- com o secret `AWS_ROLE_ARN`, assume a role por **OIDC** do GitHub (conta comum);
- sem ele, usa as **credenciais temporárias** do laboratório (AWS Academy): `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` e
  `AWS_SESSION_TOKEN`.

**Secrets e variáveis do repositório** (somente os nomes):

| Nome | Uso |
|---|---|
| `SONAR_TOKEN` | Job `qualidade` |
| `AWS_ROLE_ARN` | Autenticação por OIDC (opcional) |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` | Autenticação por credenciais temporárias (AWS Academy) |
| `MP_ACCESS_TOKEN` | Vai para o Secret do cluster; sua presença liga o provedor `mercadopago` |
| `MP_EXTERNAL_POS_ID` | Vai para o Secret do cluster |
| `MP_WEBHOOK_SECRET` | Vai para o Secret do cluster |
| `AMBIENTE_ATIVO` (variável) | Liga a publicação e o deploy |

As credenciais de AWS e a variável `AMBIENTE_ATIVO` são gravadas pelo `make github-segredos` e pelo `make up` da plataforma.
Os três secrets `MP_*` são definidos por quem opera o serviço, por exemplo:

```bash
gh secret set MP_ACCESS_TOKEN -R pesant0s/tech-challenge-f4-billing-service
gh secret set MP_EXTERNAL_POS_ID -R pesant0s/tech-challenge-f4-billing-service
gh secret set MP_WEBHOOK_SECRET -R pesant0s/tech-challenge-f4-billing-service
```

O Secret `billing-service-segredos` recebe: `MONGO_URL` (URL do MongoDB do próprio namespace), `SECRET_KEY` (SSM
`segredos/jwt_secret_key`), `RABBITMQ_URL` (SSM `rabbitmq/url`), `NEW_RELIC_LICENSE_KEY` (SSM `newrelic/license_key`, só
se o valor não for `desligado`) e, quando preenchidos, `MP_ACCESS_TOKEN`, `MP_EXTERNAL_POS_ID` e `MP_WEBHOOK_SECRET`. Os
valores sensíveis são mascarados nos logs do job. Um secret `MP_USER_ID` existe só para quem roda o script de preparação
do Mercado Pago, e o pipeline não o usa.

---

## Infraestrutura própria

Diretório `infra/` (Terraform `>= 1.10`, provider AWS `~> 5.60`). O Billing cria apenas o repositório de imagens: o
MongoDB não é um recurso AWS, roda no cluster e é descrito em `k8s/`.

| Recurso | Detalhe |
|---|---|
| `aws_ecr_repository.api` | `tech-challenge-f4-billing-service`, varredura de imagem no push, `force_delete = true` |
| `aws_ecr_lifecycle_policy.api` | Mantém as 10 últimas imagens |
| Output `ecr_url` | URL do repositório |

- **Estado remoto:** bucket S3 `tech-challenge-f4-tfstate-<conta>` (criado pela plataforma com `make bootstrap`), chave
  `billing-service/terraform.tfstate`, criptografia ligada e bloqueio por arquivo (`use_lockfile`). Bucket e região vêm de
  `-backend-config`, porque dependem da conta de quem executa (`make infra-init` faz isso).
- **Variável:** `regiao` (padrão `us-east-1`).
- **Tags** de todo recurso: `Project = tech-challenge-f4`, `Fase = 04`, `Repositorio = tech-challenge-f4-billing-service`,
  `ManagedBy = terraform`.
- **Parâmetros SSM:** o Terraform do Billing não lê nem escreve parâmetros. Quem lê o SSM é o pipeline, ao montar o
  Secret e apontar o `kubectl`: `/tech-challenge-f4/cluster/nome`, `segredos/jwt_secret_key`, `rabbitmq/url`,
  `newrelic/license_key` e `apigateway/endpoint` (teste de fumaça). A plataforma publica todos eles; o contrato está em
  [`docs/infraestrutura/contrato.md`](https://github.com/pesant0s/tech-challenge-f4-plataforma/blob/main/docs/infraestrutura/contrato.md).
- **Validar sem AWS:** `make infra-validate`.

---

## Kubernetes

Manifestos em `k8s/base`, com o overlay `k8s/overlays/producao` (kustomize). O overlay fixa o namespace
`billing-service`, acrescenta o rótulo `app.kubernetes.io/instance: producao` e recebe a imagem do pipeline no lugar de
`IMAGEM_PLACEHOLDER`. `make render` mostra o que vai ao cluster.

| Recurso | Detalhe |
|---|---|
| `Namespace` | `billing-service` |
| `ServiceAccount` | `billing-service`, sem montar o token de service account |
| `ConfigMap` `billing-service-config` | `ALGORITHM`, `ALLOWED_ORIGINS`, `LOG_FORMAT=json`, `LOG_LEVEL`, `MONGO_DB`, `NEW_RELIC_APP_NAME`, `NEW_RELIC_APPLICATION_LOGGING_FORWARDING_ENABLED=false`, `PAGAMENTO_PROVEDOR`, `PREFIXO_DOCS=/billing` |
| `Secret` `billing-service-segredos` | Criado pelo pipeline a cada deploy; nunca é versionado |
| `Deployment` `billing-service` | 2 réplicas, contêiner `api` na porta 8000, `envFrom` do ConfigMap e do Secret |
| `Service` `billing-service` | `NodePort`, porta 80 → 8000, `nodePort` fixo **30082** (o target group do NLB da plataforma aponta para ele) |
| `HorizontalPodAutoscaler` | 2 a 4 réplicas, alvo de 70% de CPU; redução com janela de estabilização de 300 s. Só CPU, porque a memória de um processo Python é quase fixa |
| `PodDisruptionBudget` | `minAvailable: 1` |
| `StatefulSet` e `Service` `mongo` | MongoDB 8.0 em replica set de 1 nó, Service headless |

**Deployment.**

- Probes: `startupProbe` em `GET /health` (a cada 5 s, até 12 falhas), `readinessProbe` em `GET /health/ready` (a cada
  10 s, 3 falhas) e `livenessProbe` em `GET /health` (a cada 20 s, 3 falhas). O liveness não toca o banco: uma queda do
  MongoDB tira o pod do balanceamento, mas não o reinicia.
- Recursos: `requests` de 100m de CPU e 256Mi de memória; `limits` de 512Mi de memória.
- Segurança: `runAsNonRoot`, UID e GID 10001, `allowPrivilegeEscalation: false`, todas as capabilities removidas,
  `automountServiceAccountToken: false`.
- Em nuvem (3 nós `t3.medium`), os pods dos serviços ficaram entre 190 e 225 MiB de memória.

**MongoDB no cluster.** `StatefulSet` `mongo` com uma réplica, `mongod --replSet rs0 --bind_ip_all`, `Service` headless
`mongo` (DNS `mongo.billing-service.svc.cluster.local`, porta 27017). A `readinessProbe` executa `rs.status()` e, se o
replica set ainda não existe, `rs.initiate` com o membro `localhost:27017`; por isso a API conecta com
`directConnection=true`. `requests` de 100m e 256Mi, `limits` de 512Mi. Os dados ficam em `emptyDir`: o ambiente é
efêmero (veja [Limitações conhecidas](#limitações-conhecidas)).

---

## Deploy na AWS

Ordem de subida de todo o sistema: **plataforma → serviços → Lambda**. A destruição segue a ordem inversa.

1. **Plataforma** (`tech-challenge-f4-plataforma`): `make bootstrap` (bucket de estado), `make up` (rede, EKS, NLB, API
   Gateway, RabbitMQ, parâmetros SSM; liga `AMBIENTE_ATIVO`) e `make github-segredos`.
2. **Serviços** (OS, Execução e Billing, em qualquer ordem): cada um com `make infra-up` e o pipeline de deploy.
3. **Lambda de autenticação** (`tech-challenge-f4-auth-lambda`): `make apply`, depois que o OS Service estiver no ar.

No Billing:

```bash
export AWS_PROFILE=<perfil da conta>       # ou as credenciais temporárias do laboratório
make conta                                 # confirma a conta AWS que receberá os recursos
make infra-up                              # cria o ECR (a plataforma precisa estar no ar)
make implantar                             # aciona o pipeline de deploy na main
```

`make implantar` dispara o workflow `ci-cd.yml` na `main`. O `make infra-up` é opcional, porque o próprio job de deploy
aplica o mesmo Terraform antes de publicar a imagem. Sem os secrets `MP_*`, o serviço sobe com o provedor simulado. Para
cobrar no Mercado Pago de verdade, grave os secrets (veja [Pipeline CI/CD](#pipeline-cicd)), prepare a loja e o caixa e,
para o webhook, cadastre a URL do gateway no painel do Mercado Pago.

Para conferir o serviço no ar: `kubectl get pods -n billing-service` (duas réplicas do Billing e o `mongo-0`) e
`<endpoint do API Gateway>/billing/docs`.

**Destruição:** derrube a Lambda e os serviços antes da plataforma (o `make down` da plataforma recusa se houver Lambda ou
banco de serviço vivos). No Billing, `make infra-down` destrói o ECR (inclusive as imagens, por causa do
`force_delete`). O namespace, o Deployment e o MongoDB desaparecem junto com o cluster.

---

## Observabilidade

- **Agente New Relic.** A imagem inclui o agente. O `entrypoint.sh` só o ativa quando `NEW_RELIC_LICENSE_KEY` está
  definida; sem licença ele fica inerte e a mesma imagem roda localmente sem conta no New Relic. O nome da aplicação é
  `tech-challenge-f4-billing-service`.
- **Logs JSON** (`LOG_FORMAT=json`, padrão no cluster), um objeto por linha no stdout, coletado pelo `nri-bundle`. Campos:
  `timestamp`, `level`, `logger`, `message`, `correlation_id` e, com o agente ativo, `trace.id` e `span.id`, que ligam a
  linha ao trace. Campos extras relevantes: `evento`, `saga_id`, `os_id`, `tipo`, `pagamento_id`, `estado`. Campos nulos são
  omitidos, porque o New Relic grava `null` como texto e o filtro `IS NULL` deixaria de casar. O formato `console` é
  legível, para o desenvolvimento.
- **Correlação.** Em requisições HTTP, o `correlation_id` vem de `x-request-id`, `x-correlation-id` ou `x-amzn-trace-id`
  (ou é gerado) e volta no cabeçalho `x-request-id` da resposta e no campo `correlation_id` dos erros. No consumo de
  mensagens, ele é o `saga_id`.
- **Traces pela fila.** O contexto do trace segue nos cabeçalhos AMQP (`traceparent`, `tracestate`, `newrelic`): o
  consumo é uma transação de background que continua o trace de quem publicou, e o outbox preserva o contexto até a
  publicação. Em nuvem, os traces atravessaram os três serviços pela fila.
- **Eventos de log** (campo `evento`): `comando_processado`, `mensagem_repetida`, `mensagem_invalida`, `falha_no_consumo`,
  `publicacao_recusada`, `mensageria_conectada`, `mensageria_reconectando`, `pagamento_conciliado`, `conciliacao_falhou`,
  `notificacao_sem_pagamento`, `webhook_assinatura_invalida` e `orcamento_enviado`.
- **Dashboard e alertas** são definidos na plataforma (`observabilidade.tf`), não aqui. Dependem dos logs do Billing: o
  gráfico "Comandos atendidos pela Execução e pelo Billing" (`evento = 'comando_processado'` por `tipo`), o gráfico de
  mensagens repetidas, descartadas ou com falha, e o alerta "Falha ao processar mensagens da Saga"
  (`logger = 'oficina.mensageria'` com `falha_no_consumo`, `mensagem_sem_saga` ou `mensagem_invalida`).
- **Acompanhar uma OS** no New Relic: `FROM Log WHERE os_id = '<os_id>'` (comandos e conciliação) e
  `FROM Log WHERE correlation_id = '<saga_id>'` (tudo o que o Billing registrou ao consumir mensagens daquela Saga).
- **Health.** `GET /health` (liveness, sem I/O) e `GET /health/ready` (confirma o MongoDB). O middleware de segurança
  acrescenta `x-content-type-options`, `x-frame-options`, `referrer-policy` e oculta o cabeçalho `server`.

---

## Decisões de arquitetura

ADRs no repositório da plataforma
([`docs/adr/`](https://github.com/pesant0s/tech-challenge-f4-plataforma/tree/main/docs/adr)):

| ADR | Decisão | Relação com o Billing |
|---|---|---|
| [ADR-017](https://github.com/pesant0s/tech-challenge-f4-plataforma/blob/main/docs/adr/ADR-017-divisao-em-microsservicos.md) | Três microsserviços divididos pelas áreas da oficina | Define o Billing como dono de orçamento, aprovação, cobrança e reembolso |
| [ADR-018](https://github.com/pesant0s/tech-challenge-f4-plataforma/blob/main/docs/adr/ADR-018-saga-orquestrada.md) | Saga orquestrada, com o orquestrador no OS Service | O Billing é participante: comandos e compensações |
| [ADR-019](https://github.com/pesant0s/tech-challenge-f4-plataforma/blob/main/docs/adr/ADR-019-rabbitmq.md) | RabbitMQ no cluster, exchanges topic, quorum queues e DLQ | Topologia e fila `billing-service.comandos` |
| [ADR-020](https://github.com/pesant0s/tech-challenge-f4-plataforma/blob/main/docs/adr/ADR-020-banco-por-servico.md) | Um banco por serviço: MongoDB no Billing | Documento por OS, atualização condicional, replica set de 1 nó |
| [ADR-021](https://github.com/pesant0s/tech-challenge-f4-plataforma/blob/main/docs/adr/ADR-021-outbox-e-idempotencia.md) | Outbox transacional e consumo idempotente | Coleções `outbox` e `mensagens_processadas`, ack manual |
| [ADR-023](https://github.com/pesant0s/tech-challenge-f4-plataforma/blob/main/docs/adr/ADR-023-mercado-pago.md) | Cobrança pelo Mercado Pago com QR Code dinâmico, atrás de uma porta | Porta `ProvedorPagamento`, adapters, conciliação e webhook |
| [ADR-024](https://github.com/pesant0s/tech-challenge-f4-plataforma/blob/main/docs/adr/ADR-024-ambientes.md) | Desenvolvimento local com docker compose, entrega na AWS | `make up` local; deploy pelo pipeline |

O ADR-022 (a Lambda de CPF consulta o OS Service) não afeta o Billing.

---

## Limitações conhecidas

- **Dados efêmeros no cluster.** O MongoDB (e o RabbitMQ) rodam com `emptyDir`: se o pod for recriado, os dados somem. No
  Billing isso inclui orçamentos, pagamentos, outbox e inbox, porque o outbox mora no mesmo MongoDB. O ambiente é efêmero
  por projeto e o EBS CSI exigiria uma role IAM própria.
- **Webhook por ambiente.** O webhook do Mercado Pago só funciona depois de cadastrar, no painel do Mercado Pago, a URL do
  API Gateway do ambiente (que muda a cada subida) e a chave de assinatura em `MP_WEBHOOK_SECRET`. Sem isso, a conciliação
  periódica confirma os pagamentos, com atraso de até `CONCILIACAO_SEGUNDOS`. A validação da assinatura não limita a
  idade do `ts`; como o Billing sempre reconsulta o pedido, uma notificação repetida não muda o estado.
- **Uma cobrança por orçamento.** Se o QR Code expira, o Billing publica `PagamentoRecusado` (`PAGAMENTO_EXPIRADO`) e a
  Saga compensa; não há novo QR Code para o mesmo orçamento.
- **Reembolso.** Só reembolso total. Se o Mercado Pago recusa, o pagamento fica `REEMBOLSO_FALHOU`, o Billing publica
  `ReembolsoFalhou` e a Saga para em `FALHA_NA_COMPENSACAO`, com alerta, sem nova tentativa automática. Um `Reembolsar`
  repetido sobre um pagamento nesse estado responde `PagamentoReembolsado` sem nova chamada ao provedor. Um reembolso feito
  direto no painel do Mercado Pago não é refletido no Billing.
- **Aviso ao cliente simulado.** O envio do orçamento só gera um log (`orcamento_enviado`); não há provedor de e-mail ou
  SMS.
- **Sem autenticação no MongoDB.** A URL do MongoDB não tem credenciais; o acesso se limita à rede do cluster (Service
  headless) e ao docker compose.
- **CORS aberto no cluster.** O ConfigMap define `ALLOWED_ORIGINS: ["*"]` (sem credenciais).
- **Conferência do contrato só com os repositórios lado a lado.** O CI clona apenas este repositório e pula a validação das
  mensagens contra o `asyncapi.yaml`; localmente, com a plataforma ao lado, ela roda.
- **Apontamentos do SonarQube.** O quality gate está aprovado, mas há 4 vulnerabilidades registradas, todas em
  `.github/workflows/ci-cd.yml` e no `Dockerfile` (veja [Testes e cobertura](#testes-e-cobertura)).
- **Portas locais em conflito.** O compose deste repositório e o `make local-up` da plataforma usam 27017 e 5672; use um
  ou o outro.
