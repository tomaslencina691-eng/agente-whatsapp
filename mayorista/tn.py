"""El cliente de Tiendanube.

Tiendanube aporta el nombre, las fotos, las categorías y los talles de cada
modelo. Por defecto es solo lectura y el stock del mayorista es propio (ver
almacen.py). Con el «stock compartido» activado (ver stock_tn.py) también se
escribe el stock de las variantes, y entonces la app necesita el permiso de
escribir productos. Habla la API con `urllib`, como el resto del proyecto.
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


# -- Escribir el stock (solo con el stock compartido) -----------------------------


def _cambiar_stock(producto: int, variante: int, accion: str, valor: int) -> int:
    """Cambia el stock de UNA variante y devuelve el que quedó. `accion` es "replace" o "variation".

    Con "variation" Tiendanube suma o resta en su lado (no pisa una venta que entró en el medio).
    Ojo: si se resta más de lo que hay, Tiendanube no da error: deja el stock en 0.
    """
    actualizadas = pedir("POST", f"/products/{producto}/variants/stock", {"action": accion, "value": valor, "id": variante})
    for v in actualizadas if isinstance(actualizadas, list) else []:
        if v.get("id") == variante:
            return int(v["stock"]) if v.get("stock") is not None else 0
    raise ErrorDeTiendanube(f"Tiendanube no devolvió la variante {variante} del modelo {producto} después de cambiar el stock.")


def ajustar_stock(producto: int, variante: int, cambio: int) -> int:
    """Suma (o resta, si es negativo) unidades al stock de una variante. Devuelve el stock que quedó."""
    return _cambiar_stock(producto, variante, "variation", cambio)


def fijar_stock(producto: int, variante: int, cantidad: int) -> int:
    """Deja el stock de una variante en `cantidad`. Devuelve el stock que quedó."""
    return _cambiar_stock(producto, variante, "replace", cantidad)


# -- Crear productos (el alta desde el panel) ------------------------------------


def crear_producto(cuerpo: dict) -> dict:
    """Crea un producto con sus variantes. Devuelve el producto como lo guardó Tiendanube."""
    return pedir("POST", "/products", cuerpo)


def subir_foto(producto: int, base64_: str, nombre: str, posicion: int) -> dict:
    """Sube una foto al producto: el archivo va en base64, sin el prefijo "data:...;base64,"."""
    return pedir("POST", f"/products/{producto}/images", {"attachment": base64_, "filename": nombre, "position": posicion})


def borrar_producto(producto: int) -> None:
    pedir("DELETE", f"/products/{producto}")


def publicar(producto: int, publicado: bool = True) -> dict:
    return pedir("PUT", f"/products/{producto}", {"published": bool(publicado)})


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
