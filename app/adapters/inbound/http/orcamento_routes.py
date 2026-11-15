from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends

from app.adapters.inbound.http.dependencias import obter_servicos
from app.adapters.inbound.http.orcamento_schemas import OrcamentoResponse
from app.domain.exceptions import NotFoundException
from app.domain.ports.provedor_pagamento import SituacaoPagamento
from app.infrastructure.security import Usuario, get_funcionario, get_usuario

router = APIRouter(prefix="/billing", tags=["💳 Orçamento e Pagamento"])

SITUACOES_DO_SIMULADOR = {
    "aprovar": SituacaoPagamento.PAGO,
    "recusar": SituacaoPagamento.RECUSADO,
    "expirar": SituacaoPagamento.EXPIRADO,
}


def _titular(usuario: Usuario) -> str | None:
    """Id do cliente a que o acesso se restringe; a oficina enxerga todos os orçamentos."""
    return usuario.sub if usuario.cliente else None


@router.get("/orcamentos/{os_id}", response_model=OrcamentoResponse,
    summary="Orçamento da OS e seu pagamento",
    description="Funcionário vê qualquer orçamento; cliente só o próprio (o de outro cliente responde 404).")
def obter_orcamento(os_id: UUID, usuario: Usuario = Depends(get_usuario), servicos=Depends(obter_servicos)):
    return OrcamentoResponse.montar(*servicos.decisao.obter(str(os_id), _titular(usuario)))


@router.post("/orcamentos/{os_id}/aprovar", response_model=OrcamentoResponse,
    summary="Aprovar o orçamento",
    description="Cliente titular ou funcionário. Orçamento vencido responde 422.")
def aprovar_orcamento(os_id: UUID, usuario: Usuario = Depends(get_usuario), servicos=Depends(obter_servicos)):
    por = "CLIENTE" if usuario.cliente else "FUNCIONARIO"
    return OrcamentoResponse.montar(servicos.decisao.aprovar(str(os_id), por, _titular(usuario)))


@router.post("/orcamentos/{os_id}/rejeitar", response_model=OrcamentoResponse,
    summary="Rejeitar o orçamento", description="Cliente titular ou funcionário.")
def rejeitar_orcamento(os_id: UUID, usuario: Usuario = Depends(get_usuario), servicos=Depends(obter_servicos)):
    return OrcamentoResponse.montar(servicos.decisao.rejeitar(str(os_id), _titular(usuario)))


@router.post("/simulador/pagamentos/{pagamento_id}/{acao}", status_code=204,
    summary="Simular o pagamento (aprovar, recusar ou expirar)",
    description="Só com PAGAMENTO_PROVEDOR=simulado. A conciliação publica o resultado, como faria com o provedor real.")
def simular_pagamento(pagamento_id: str, acao: Literal["aprovar", "recusar", "expirar"],
                      _=Depends(get_funcionario), servicos=Depends(obter_servicos)):
    if servicos.provedor.nome != "SIMULADO":
        raise NotFoundException("Simulador desligado")
    servicos.provedor.definir_situacao(pagamento_id, SITUACOES_DO_SIMULADOR[acao])
