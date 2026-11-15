from decimal import ROUND_HALF_UP, Decimal


def dinheiro(valor: Decimal) -> str:
    """Valor monetário como texto com duas casas, o formato do contrato."""
    return str(valor.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
