"""Cuánto cuesta un pedido cuando el negocio tiene líneas de producto.

Una línea es un grupo de categorías con su propio mínimo y su propia escala de
precios (por ejemplo G5, Top Quality, Shorts). Las unidades se cuentan dentro de
cada línea: no se mezclan calidades para llegar a un precio mejor, pero sí se
mezclan modelos de una misma línea. Cada línea que se pide tiene que llegar a su
mínimo.

Sin líneas (`lineas` vacío) todo sigue como siempre: una sola escala para todo el
pedido (ver `sitio.precio_unidad`). Lo que no entra en ninguna línea se cobra con
esa escala general.

La web (static/index.html) repite esta misma cuenta en JavaScript para mostrar el
total antes de enviar: si cambia una, hay que cambiar la otra.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable


def linea_de(cats: Iterable[str], lineas: list[dict]) -> dict | None:
    """La primera línea (en el orden del panel) que incluye alguna de las categorías del modelo."""
    cats = set(cats)
    return next((l for l in lineas if cats & set(l.get("cats") or [])), None)


def precio_de_linea(linea: dict, unidades: int) -> int | None:
    """El precio por unidad de la escala que corresponde a tantas unidades; None si no llega ni a la primera."""
    escalas = sorted(linea.get("tramos") or [], key=lambda t: t["desde"], reverse=True)
    return next((t["precio"] for t in escalas if unidades >= t["desde"]), None)


def total_del_pedido(
    items: list[tuple[list[str], int]],
    lineas: list[dict],
    precio_general: Callable[[int], int | None],
) -> int | None:
    """El total estimado de un pedido: [(categorías del modelo, cantidad)].

    None si alguna parte no tiene precio (una línea que no llegó a su primera escala, o
    modelos sin línea cuando la escala general tampoco los cubre): lo confirma una persona.
    """
    por_linea: dict[str, int] = {}
    sin_linea = 0
    for cats, cantidad in items:
        l = linea_de(cats, lineas)
        if l is None:
            sin_linea += cantidad
        else:
            por_linea[l["id"]] = por_linea.get(l["id"], 0) + cantidad

    total = 0
    for l in lineas:
        n = por_linea.get(l["id"], 0)
        if not n:
            continue
        precio = precio_de_linea(l, n)
        if precio is None:
            return None
        total += precio * n
    if sin_linea:
        precio = precio_general(sin_linea)
        if precio is None:
            return None
        total += precio * sin_linea
    return total
