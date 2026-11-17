"""Cruzar los cobros de Mercado Pago con los pedidos de la web.

Un cobro (una transferencia que entró) y un pedido «nuevo» van juntos cuando:

  · el monto es EXACTAMENTE el total del pedido (el total estimado que calculó la web);
  · el cobro no es de mucho antes que el pedido (no puede pagarse algo que todavía no se pidió);
  · y la pareja es única: ese cobro solo encaja con ese pedido, y ese pedido solo con ese cobro.

Si hay dudas (dos pedidos con el mismo total, o dos cobros del mismo monto) no se
decide nada: lo resuelve una persona desde el panel. Esto es lo único que se
automatiza; nunca se confirma un pedido sin un cobro real de Mercado Pago atrás.
"""

from __future__ import annotations

from datetime import datetime, timedelta

# Un cobro puede ser unas horas anterior al pedido (el cliente transfiere mientras arma el mensaje).
TOLERANCIA_ANTES_DEL_PEDIDO = timedelta(hours=2)


def _fecha(texto: str) -> datetime:
    return datetime.fromisoformat(str(texto).replace("Z", "+00:00"))


def coincide(cobro: dict, pedido: dict) -> bool:
    """¿Este cobro podría ser el pago de este pedido?"""
    if pedido.get("estado") != "nuevo" or pedido.get("total") is None:
        return False
    if round(float(cobro["monto"]), 2) != round(float(pedido["total"]), 2):
        return False
    try:
        return _fecha(cobro["fecha"]) >= _fecha(pedido["fecha"]) - TOLERANCIA_ANTES_DEL_PEDIDO
    except ValueError:
        return False


def cruzar(cobros: list[dict], pedidos: list[dict]) -> dict[str, dict]:
    """Por cada cobro sin asignar: los pedidos que podrían ser, y si la pareja es segura.

    Devuelve {id del cobro: {"pedidos": [códigos], "seguro": código o None}}.
    """
    posibles = {c["id"]: [p["codigo"] for p in pedidos if coincide(c, p)] for c in cobros}
    por_pedido: dict[str, list[str]] = {}
    for cobro_id, codigos in posibles.items():
        for codigo in codigos:
            por_pedido.setdefault(codigo, []).append(cobro_id)
    salida: dict[str, dict] = {}
    for cobro_id, codigos in posibles.items():
        seguro = codigos[0] if len(codigos) == 1 and len(por_pedido[codigos[0]]) == 1 else None
        salida[cobro_id] = {"pedidos": codigos, "seguro": seguro}
    return salida
