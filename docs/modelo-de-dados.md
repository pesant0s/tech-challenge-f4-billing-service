# Modelo de dados

Coleções do MongoDB do Billing (banco `billing`). Cada comando e cada decisão gravam estado, outbox e
inbox numa única transação, o que exige replica set (ADR-020 e ADR-021). Dinheiro fica como texto com
duas casas (`"150.00"`), como no contrato das mensagens; datas ficam em UTC.

## Diagrama

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

## Coleções

| Coleção | Papel |
|---|---|
| `orcamentos` | Um por OS. Nasce `GERADO` com validade de `ORCAMENTO_VALIDADE_MINUTOS`. |
| `pagamentos` | Um por orçamento aprovado e cobrado. O `_id` é derivado do orçamento, então repetir a cobrança reaproveita a chave de idempotência no provedor. |
| `outbox` | Eventos a publicar em `oficina.eventos`. O relay reivindica cada mensagem com `find_one_and_update` (`reservada_ate`), para que duas réplicas não publiquem a mesma. |
| `mensagens_processadas` | Inbox: o `_id` é o `message_id`; uma chave duplicada marca a mensagem como repetida e ela não tem efeito. |
| `simulador_pagamentos` | Situação das cobranças do provedor simulado. Só é usada com `PAGAMENTO_PROVEDOR=simulado`. |

## Índices

Criados na subida do serviço.

| Coleção | Índice | Motivo |
|---|---|---|
| `orcamentos` | `os_id` (único) | Um orçamento por OS; consulta pela rota e pelos comandos. |
| `orcamentos` | `estado` | Busca dos orçamentos vencidos na conciliação. |
| `pagamentos` | `orcamento_id` (único) | Um pagamento por orçamento. |
| `pagamentos` | `provedor_ref` | Webhook do Mercado Pago. |
| `pagamentos` | `estado` | Busca dos pagamentos pendentes na conciliação. |
| `outbox` | `(publicado_em, criado_em)` | Relay lê as pendentes em ordem de criação. |

## Estados

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

Toda mudança de estado grava com filtro pelo estado anterior; quem perde a corrida entre réplicas não
publica o evento.
