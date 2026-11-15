"""Cria a loja e o caixa do Mercado Pago exigidos pelo QR dinâmico (ADR-023) e imprime o `external_pos_id`."""
import argparse
import os
import sys
import uuid

import httpx

URL = os.environ.get("MP_API_URL", "https://api.mercadopago.com")
LOJA = "oficina-loja-1"


def main() -> int:
    parser = argparse.ArgumentParser(description="Cria a loja e o caixa do Mercado Pago e imprime o external_pos_id. Lê MP_ACCESS_TOKEN e MP_USER_ID do ambiente.")
    parser.add_argument("external_id", nargs="?", default="oficinacaixa1",
                        help="external_id do caixa, só letras e números (padrão: oficinacaixa1)")
    argumentos = parser.parse_args()
    token, user_id = os.environ.get("MP_ACCESS_TOKEN"), os.environ.get("MP_USER_ID")
    if not (token and user_id):
        print("Defina MP_ACCESS_TOKEN e MP_USER_ID.", file=sys.stderr)
        return 1
    with httpx.Client(base_url=URL, timeout=10, headers={"Authorization": f"Bearer {token}"}) as http:
        # Rodar de novo reaproveita a loja e o caixa: o external_id é único na conta.
        lojas = http.get(f"/users/{user_id}/stores/search", params={"external_id": LOJA}).json().get("results", [])
        if not lojas:
            loja = http.post(f"/users/{user_id}/stores", json={
                "name": "Oficina", "external_id": LOJA,
                "location": {"street_number": "100", "street_name": "Rua da Oficina", "city_name": "São Paulo",
                             "state_name": "São Paulo", "latitude": -23.55, "longitude": -46.63, "reference": "Sede"},
            })
            loja.raise_for_status()
            lojas = [loja.json()]
        caixa = http.post("/v2/pos", headers={"X-Idempotency-Key": str(uuid.uuid4())}, json={
            "name": "Caixa 1", "store_id": str(lojas[0]["id"]), "external_id": argumentos.external_id,
        })
        if caixa.status_code >= 400 and "pos_already_exists" not in caixa.text:
            print(caixa.text, file=sys.stderr)
            caixa.raise_for_status()
    print(argumentos.external_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
