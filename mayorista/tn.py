"""El cliente de Tiendanube: solo lectura.

Tiendanube aporta el nombre, las fotos, las categorías y los talles de cada
modelo. El stock del mayorista NO sale de acá: es propio (ver almacen.py), y
a Tiendanube no se le escribe nada, así que la app alcanza con permisos de
lectura. Habla la API con `urllib`, como el resto del proyecto.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request

# Tiendanube pide que el User-Agent diga quién es la app y cómo contactarla.
_USER_AGENT = "Mayorista (%s)" % (os.getenv("TIENDANUBE_CONTACTO", "").strip() or "sin contacto")


log = logging.getLogger("mayorista.tn")

TIENDA = os.getenv("TIENDANUBE_STORE_ID", "").strip()
TOKEN = os.getenv("TIENDANUBE_ACCESS_TOKEN", "").strip()

# La API de Tiendanube acepta hasta 30 ids por consulta.
MAXIMO_DE_IDS = 30


class ErrorDeTiendanube(Exception):
    """Tiendanube contestó algo que no esperábamos. El mensaje es para mostrar."""


def configurado() -> bool:
    return bool(TIENDA and TOKEN)


def pedir(metodo: str, ruta: str, cuerpo: list | dict | None = None, reintentos: int = 3):
    """Un pedido a la API. Reintenta si Tiendanube pide esperar (429)."""
    if not configurado():
        raise ErrorDeTiendanube("Faltan TIENDANUBE_STORE_ID y TIENDANUBE_ACCESS_TOKEN.")

    for intento in range(reintentos + 1):
        pedido = urllib.request.Request(
            f"https://api.tiendanube.com/v1/{TIENDA}{ruta}",
            data=json.dumps(cuerpo).encode("utf-8") if cuerpo is not None else None,
            method=metodo,
            headers={
                # Tiendanube no usa "Authorization": su header se llama Authentication.
                "Authentication": f"bearer {TOKEN}",
                "User-Agent": _USER_AGENT,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(pedido, timeout=40) as respuesta:
                crudo = respuesta.read().decode("utf-8")
                return json.loads(crudo) if crudo else {}
        except urllib.error.HTTPError as e:
            detalle = e.read().decode("utf-8", "replace")[:400]
            if e.code == 429 and intento < reintentos:
                time.sleep(2 * (intento + 1))
                continue
            raise ErrorDeTiendanube(f"Tiendanube devolvió {e.code} en {metodo} {ruta}: {detalle}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if intento < reintentos:
                time.sleep(1 + intento)
                continue
            raise ErrorDeTiendanube(f"No se pudo hablar con Tiendanube: {e}") from None
    raise ErrorDeTiendanube("Tiendanube no contestó.")  # pragma: no cover


# -- Leer ---------------------------------------------------------------------


def todos_los_productos() -> list[dict]:
    """Todos los productos de la tienda, publicados o no."""
    productos: list[dict] = []
    for pagina in range(1, 21):
        lote = pedir("GET", f"/products?per_page=200&page={pagina}")
        productos += lote
        if len(lote) < 200:
            break
    return productos


def productos_por_id(ids: list[int]) -> dict[int, dict]:
    """Los productos con esos ids, tal como están ahora."""
    salida: dict[int, dict] = {}
    ids = sorted(set(ids))
    for i in range(0, len(ids), MAXIMO_DE_IDS):
        trozo = ids[i : i + MAXIMO_DE_IDS]
        for p in pedir("GET", f"/products?ids={','.join(map(str, trozo))}&per_page=200"):
            salida[p["id"]] = p
    return salida


def categorias() -> list[dict]:
    return pedir("GET", "/categories?per_page=200")


# -- Fotos --------------------------------------------------------------------

CABECERAS_CDN = {
    # La CDN devuelve 403 si el pedido no parece de un navegador.
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Referer": os.getenv("TIENDA_URL", "").strip() or "https://www.tiendanube.com/",
    "Accept": "image/webp,image/*",
}
_fotos: dict[str, bytes] = {}


def bajar_foto(url: str) -> bytes:
    """Una foto de la CDN de Tiendanube, guardada en memoria después de la primera vez."""
    if url not in _fotos:
        with urllib.request.urlopen(urllib.request.Request(url, headers=CABECERAS_CDN), timeout=30) as r:
            _fotos[url] = r.read()
    return _fotos[url]


def miniatura(url: str, ancho: int = 480) -> str:
    """La misma foto, en un tamaño chico (WebP): pesa unos 40 KB."""
    import re

    return re.sub(r"-\d+-\d+\.(png|jpe?g|webp)$", f"-{ancho}-0.webp", url)
