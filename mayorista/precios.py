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

import math
from collections.abc import Callable, Iterable

# Con el tope por talle, un talle siempre puede llevar al menos estas camisetas,
# aunque el porcentaje dé menos (6 camisetas al 30 % darían 1).
MINIMO_POR_TALLE = 2

# Los nombres que usa la tienda para el mismo talle.
_SINONIMOS = {"2XL": "XXL", "3XL": "XXXL", "4XL": "XXXXL"}


def linea_de(cats: Iterable[str], lineas: list[dict]) -> dict | None:
    """La primera línea (en el orden del panel) que incluye alguna de las categorías del modelo."""
    cats = set(cats)
    return next((l for l in lineas if cats & set(l.get("cats") or [])), None)


def precio_de_linea(linea: dict, unidades: int) -> int | None:
    """El precio por unidad de la escala que corresponde a tantas unidades; None si no llega ni a la primera."""
    escalas = sorted(linea.get("tramos") or [], key=lambda t: t["desde"], reverse=True)
    return next((t["precio"] for t in escalas if unidades >= t["desde"]), None)


def variante_de(linea: dict, lineas: list[dict]) -> dict:
    """La variante «con parche y número» de una línea, o la misma línea si no tiene."""
    return next((v for v in lineas if v.get("personalizadaDe") == linea["id"]), linea)


def total_del_pedido(
    items: list[tuple],
    lineas: list[dict],
    precio_general: Callable[[int], int | None],
) -> int | None:
    """El total estimado de un pedido: [(categorías del modelo, cantidad[, con parche y número])].

    None si alguna parte no tiene precio (una línea que no llegó a su primera escala, o
    modelos sin línea cuando la escala general tampoco los cubre): lo confirma una persona.
    """
    por_linea: dict[str, int] = {}
    sin_linea = 0
    for cats, cantidad, *resto in items:
        l = linea_de(cats, lineas)
        if l is None:
            sin_linea += cantidad
        else:
            if resto and resto[0]:
                l = variante_de(l, lineas)
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


# -- Pedido parejo por talle ------------------------------------------------------
#
# Dentro de cada línea (calidad), un mismo talle no puede ser más que `tope` % de
# las camisetas de esa línea, con un piso de MINIMO_POR_TALLE. Los modelos se
# mezclan libremente: lo que se cuenta es el talle. Así nadie se lleva 12 L.
# La web repite esta cuenta en JavaScript (static/index.html).


def talle_base(talle: str) -> str:
    """El talle sin el nombre ni el número: «L, Di María 22» es una L; «2XL» es XXL."""
    base = str(talle or "").split(",")[0].strip().upper()
    return _SINONIMOS.get(base, base)


def maximo_por_talle(total: int, tope: int) -> int:
    """Cuántas camisetas de un mismo talle se pueden llevar en una línea con `total` camisetas."""
    return max(MINIMO_POR_TALLE, math.floor(total * tope / 100))


def talles_desparejos(items: list[tuple], lineas: list[dict], tope: int, nombre_sin_linea: str = "Otros modelos") -> list[dict]:
    """Los talles que se pasan del tope: [(categorías, talle, cantidad[, con parche y número])].

    Devuelve [{linea, talle, cantidad, maximo, total}] (vacío si el pedido es parejo o no hay tope).
    """
    if not tope:
        return []
    por_linea: dict[str, dict[str, int]] = {}
    nombres: dict[str, str] = {}
    for cats, talle, cantidad, *resto in items:
        l = linea_de(cats, lineas)
        if l is not None and resto and resto[0]:
            l = variante_de(l, lineas)
        clave = l["id"] if l else ""
        nombres[clave] = l["nombre"] if l else nombre_sin_linea
        talles = por_linea.setdefault(clave, {})
        base = talle_base(talle)
        talles[base] = talles.get(base, 0) + cantidad
    salida = []
    for clave, talles in por_linea.items():
        total = sum(talles.values())
        maximo = maximo_por_talle(total, tope)
        for talle, n in talles.items():
            if n > maximo:
                salida.append({"linea": nombres[clave], "talle": talle, "cantidad": n, "maximo": maximo, "total": total})
    return salida
