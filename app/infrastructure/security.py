from dataclasses import dataclass
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from app.infrastructure.config import settings

# O token é emitido pelo OS Service (e pela Lambda, para clientes) com a mesma SECRET_KEY; aqui só se
# confere a assinatura e os claims, sem banco de usuários.
bearer = HTTPBearer(scheme_name="Token", description="Token de funcionário (POST /auth/token do OS Service) ou de cliente (Lambda de autenticação)", auto_error=False)

PAPEIS = ("ADMIN", "ATENDENTE")


@dataclass(frozen=True)
class Usuario:
    sub: str
    cliente: bool


def _credenciais_invalidas() -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, "Credenciais inválidas ou token expirado",
                         {"WWW-Authenticate": "Bearer"})


def get_usuario(credenciais: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Usuario:
    """Funcionário (`tipo=usuario` com papel da oficina) ou cliente (`tipo=cliente`, `sub` = id do cliente)."""
    if credenciais is None:
        raise _credenciais_invalidas()
    try:
        claims = jwt.decode(credenciais.credentials, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except JWTError:
        raise _credenciais_invalidas()
    if not claims.get("sub"):
        raise _credenciais_invalidas()
    if claims.get("tipo") == "cliente":
        return Usuario(sub=claims["sub"], cliente=True)
    if claims.get("tipo") != "usuario" or claims.get("role") not in PAPEIS:
        raise _credenciais_invalidas()
    return Usuario(sub=claims["sub"], cliente=False)


def get_funcionario(usuario: Usuario = Depends(get_usuario)) -> Usuario:
    if usuario.cliente:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Token de cliente não dá acesso a esta rota")
    return usuario
