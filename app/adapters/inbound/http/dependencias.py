from fastapi import Request


def obter_servicos(request: Request):
    return request.app.state.servicos
