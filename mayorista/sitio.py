"""La web mayorista: catálogo con carrito que manda el pedido por WhatsApp.

    python sitio.py

Es un catálogo con carrito que manda el pedido por WhatsApp. El stock es PROPIO
del mayorista (ver almacen.py): se carga y se descuenta desde el panel de
administración, y no se mezcla con el de la tienda. De Tiendanube solo se leen,
cada pocos minutos, el nombre, las fotos y las categorías de cada modelo.

Qué hace este servidor, en cuatro cosas:

  · GET  /api/catalogo  los modelos publicados que están en el mayorista y
                        tienen stock, con sus talles y cuántas unidades quedan, y
                        los ajustes de la web (textos, precios, color, número...).
                        Se editan desde el panel; lo que no se tocó viene de
                        negocio.json (ver ajustes.py).
  · POST /api/pedido    guarda el pedido que armó la persona, con un código que
                        viaja en el mensaje de WhatsApp. El stock NO se descuenta
                        acá: se descuenta cuando el equipo confirma el pedido
                        desde el panel.
  · GET  /img/<id>/<n>  la foto del producto. Pasa por acá porque la CDN de
                        Tiendanube rechaza los pedidos que no parecen de un
                        navegador, y así además se guarda en memoria.
  · GET  /              la página (static/index.html).
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import admin
import agente_cliente
import ajustes
import almacen
import avisos
import historial as hist
import precios
import stock_tn
import tn

log = logging.getLogger("mayorista")

CARPETA = Path(__file__).parent
PUERTO = int(os.getenv("PUERTO", "8000"))

CACHE_SEGUNDOS = int(os.getenv("MAYORISTA_CACHE_SEGUNDOS", "300"))
# Con "true" no se puede pedir más unidades de un talle que las que hay.
LIMITAR_STOCK = os.getenv("MAYORISTA_LIMITAR_STOCK", "true").strip().lower() in ("1", "true", "si", "yes")
# Solo para la primera vez: con el stock propio vacío, copia el de la tienda (ver admin.filas_iniciales).
CARGAR_STOCK_INICIAL = os.getenv("MAYORISTA_CARGAR_STOCK_INICIAL", "").strip().lower() in ("1", "true", "si", "yes")
TIENDA_URL = os.getenv("TIENDA_URL", "").strip()
if not TIENDA_URL.lower().startswith(("http://", "https://")):
    TIENDA_URL = ""


def _leer_negocio() -> dict:
    """Marca, precios, categorías y condiciones: lo que cambia de un cliente a otro.

    Los define `negocio.json`; si no está, rige `negocio.ejemplo.json` (con
    precios de ejemplo: nunca se publica así).
    """
    for nombre in ("negocio.json", "negocio.ejemplo.json"):
        ruta = CARPETA / nombre
        if ruta.is_file():
            return json.loads(ruta.read_text(encoding="utf-8"))
    return {}


NEGOCIO = _leer_negocio()
MARCA = str(NEGOCIO.get("marca") or "").strip()
WHATSAPP = os.getenv("MAYORISTA_WHATSAPP", "").strip() or str(NEGOCIO.get("whatsapp") or "").strip()

# Precio por unidad según el total de camisetas del pedido. Del más alto al más bajo.
TRAMOS = sorted(
    ({"desde": int(t["desde"]), "precio": int(t["precio"])} for t in NEGOCIO.get("tramos") or []),
    key=lambda t: t["desde"],
    reverse=True,
)
MINIMO = int(NEGOCIO.get("minimo", 6))
CATEGORIAS = NEGOCIO.get("categorias") or []

ORDEN_DE_TALLES = ["XS", "S", "M", "L", "XL", "XXL", "XXXL"]

_LIMPIAR = [
    r"\s*\|.*$",
    r"\s*[-–]\s*Versi[oó]n (Jugador|Hincha)(\s+FODEN.*)?",
    r"\s*\(Versi[oó]n Jugador\)",
    r"\s*[-–]\s*Con Parche F[uú]tbol Argentino",
    r"^Camiseta\s+",
]

_candado = threading.Lock()
# Lo que se lee de Tiendanube (nombre, fotos, categorías): se guarda unos minutos.
# El stock NO está acá: se lee de la base en cada pedido a /api/catalogo.
_cache: dict = {"t": 0.0, "productos": None, "imagenes": {}}


# -- Tiendanube ---------------------------------------------------------------


def _traer_productos() -> list[dict]:
    return tn.todos_los_productos()


# -- Armar el catálogo --------------------------------------------------------


def nombre_limpio(nombre: str) -> str:
    """Saca del nombre lo que la web muestra aparte (la versión, los apéndices)."""
    for patron in _LIMPIAR:
        nombre = re.sub(patron, "", nombre, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", nombre).strip(" -–")


_categorias_cache: dict = {"t": 0.0, "valor": None}


def categorias_vigentes() -> list[dict]:
    """Los filtros de la web: los que se editan en el panel o, si nunca se tocaron, los de negocio.json.

    Se guardan 15 segundos para no ir a la base en cada producto.
    """
    if _almacen is None:
        return CATEGORIAS
    if _categorias_cache["valor"] is not None and time.time() - _categorias_cache["t"] < 15:
        return _categorias_cache["valor"]
    try:
        guardadas = _almacen.config("categorias")
    except Exception:  # noqa: BLE001
        log.warning("No se pudieron leer las categorías de la base: se usan las últimas conocidas.")
        return _categorias_cache["valor"] if _categorias_cache["valor"] is not None else CATEGORIAS
    valor = CATEGORIAS if guardadas is None else guardadas
    _categorias_cache.update(t=time.time(), valor=valor)
    return valor


def categorias_de(producto: dict) -> list[str]:
    """Los ids de categoría del negocio a los que pertenece."""
    return admin.categorias_del_producto(producto, categorias_vigentes())


_web_cache: dict = {"t": 0.0, "valor": None}


def _ajustes_de_siempre() -> dict:
    return ajustes.por_defecto(NEGOCIO, MARCA, WHATSAPP, TIENDA_URL, LIMITAR_STOCK, MINIMO, TRAMOS)


def web_vigente() -> dict:
    """Los ajustes de la web: los de negocio.json y del entorno, con lo guardado desde el panel encima.

    Se guardan 15 segundos para no ir a la base en cada visita.
    """
    base = _ajustes_de_siempre()
    if _almacen is None:
        return base
    if _web_cache["valor"] is not None and time.time() - _web_cache["t"] < 15:
        return _web_cache["valor"]
    try:
        guardados = _almacen.config("web")
    except Exception:  # noqa: BLE001
        log.warning("No se pudieron leer los ajustes de la web: se usan los últimos conocidos.")
        return _web_cache["valor"] if _web_cache["valor"] is not None else base
    valor = ajustes.combinar(base, guardados)
    _web_cache.update(t=time.time(), valor=valor)
    return valor


def version_de(nombre: str) -> str:
    """"jugador" si el nombre lo dice; si no, "fan"."""
    return "jugador" if re.search(r"versi[oó]n jugador|tela jugador", nombre, re.IGNORECASE) else "fan"


def _clave_de_talle(talle: str) -> tuple[int, str]:
    base = talle.split(" - ")[0].strip().upper()
    return (ORDEN_DE_TALLES.index(base) if base in ORDEN_DE_TALLES else 99, talle)


def _fotos_de(p: dict) -> list[str]:
    fotos = []
    for imagen in sorted(p.get("images") or [], key=lambda i: i.get("position") or 0)[:2]:
        src = imagen.get("src") or ""
        if src:
            # 480 px alcanza para la grilla y pesa unos 40 KB.
            fotos.append(re.sub(r"-\d+-\d+\.(png|jpe?g|webp)$", "-480-0.webp", src))
    return fotos


def _imagenes_de(productos: list[dict]) -> dict[int, list[str]]:
    return {p["id"]: f for p in productos if p.get("published") and (f := _fotos_de(p))}


def stock_por_modelo(filas: list[dict]) -> dict[int, dict[str, int]]:
    """Las filas de la base, agrupadas: {modelo: {talle: cantidad}}."""
    salida: dict[int, dict[str, int]] = {}
    for f in filas:
        salida.setdefault(f["producto_id"], {})[f["talle"]] = f["cantidad"]
    return salida


def _talles_de_tiendanube(p: dict) -> list[dict]:
    """Solo sin base propia (desarrollo): el stock de la tienda, como antes."""
    talles = []
    for v in p.get("variants") or []:
        stock = v.get("stock")
        # stock None = la variante no lleva control de stock: no se ofrece
        # (en mayorista hace falta saber cuántas hay).
        if stock is None or stock <= 0:
            continue
        etiqueta = ", ".join(x.get("es", "") for x in v.get("values") or [] if x.get("es")) or "Único"
        talles.append({"talle": etiqueta, "stock": int(stock)})
    return talles


def armar_catalogo(productos: list[dict], stock: dict[int, dict[str, int]] | None = None) -> tuple[list[dict], dict[int, list[str]]]:
    """Los modelos publicados que tienen stock. Devuelve (catálogo, urls de fotos).

    `stock` es el stock propio del mayorista ({modelo: {talle: cantidad}}): solo
    entran los modelos que figuran ahí. Sin `stock` (sin base, solo en desarrollo)
    se usa el de Tiendanube.
    """
    catalogo: list[dict] = []
    imagenes: dict[int, list[str]] = {}

    for p in productos:
        if not p.get("published"):
            continue

        if stock is None:
            talles = _talles_de_tiendanube(p)
        else:
            talles = [{"talle": t, "stock": int(n)} for t, n in (stock.get(p["id"]) or {}).items() if n > 0]
        if not talles:
            continue
        talles.sort(key=lambda t: _clave_de_talle(t["talle"]))

        fotos = _fotos_de(p)
        if not fotos:
            continue

        nombre = (p.get("name") or {}).get("es") or "Camiseta"
        imagenes[p["id"]] = fotos
        catalogo.append(
            {
                "id": p["id"],
                "nombre": nombre_limpio(nombre),
                "cats": categorias_de(p),
                "version": version_de(nombre),
                "fotos": len(fotos),
                "talles": talles,
            }
        )
    return catalogo, imagenes


def _actualizar_si_hace_falta() -> None:
    """Vuelve a leer de Tiendanube si lo que hay guardado ya está viejo."""
    with _candado:
        if _cache["productos"] is not None and time.time() - _cache["t"] < CACHE_SEGUNDOS:
            return
        try:
            productos = _traer_productos()
        except Exception as e:  # noqa: BLE001
            log.warning("No se pudo leer Tiendanube: %s", e)
            _avisador.fallo_la_tienda(f"{type(e).__name__}: {e}")
            if _cache["productos"] is None:
                raise
            # Hay una versión anterior: mejor mostrarla que dejar la web caída.
            _cache["t"] = time.time() - CACHE_SEGUNDOS + 30
            return
        _cache.update(t=time.time(), productos=productos, imagenes=_imagenes_de(productos))


def _catalogo_actual() -> list[dict]:
    """El catálogo de ahora: nombres y fotos de la caché, stock fresco de la base."""
    _actualizar_si_hace_falta()
    stock = stock_por_modelo(_almacen.filas()) if _almacen is not None else None
    catalogo = armar_catalogo(_cache["productos"], stock)[0]
    lineas = web_vigente()["lineas"]
    for p in catalogo:
        linea = precios.linea_de(p["cats"], lineas)
        p["linea"] = linea["id"] if linea else None
    orden = web_vigente()["orden"]
    if orden == "stock":
        catalogo.sort(key=lambda p: -sum(t["stock"] for t in p["talles"]))
    elif orden == "nombre":
        catalogo.sort(key=lambda p: p["nombre"].lower())
    else:  # novedades: los ids de la tienda crecen con el tiempo
        catalogo.sort(key=lambda p: -p["id"])
    return catalogo


def precio_unidad(unidades: int) -> int | None:
    """El precio por unidad que corresponde a un pedido de tantas camisetas."""
    tramos = sorted(web_vigente()["tramos"], key=lambda t: t["desde"], reverse=True)
    return next((t["precio"] for t in tramos if unidades >= t["desde"]), None)


# -- La app -------------------------------------------------------------------

_historial = hist.crear()
_almacen = stock_tn.AlmacenConTiendanube(_base) if (_base := almacen.crear()) is not None else None
_agente = agente_cliente.crear()
# Los mails de aviso (un pedido nuevo, pocas unidades...): se los pide al agente, que tiene el correo.
_avisador = avisos.Avisador(_almacen, _agente)


def _invalidar_cache() -> None:
    """Después de incluir un modelo o tocar las categorías, la web vuelve a leer todo ya."""
    _cache["t"] = 0.0
    _categorias_cache["t"] = 0.0
    _categorias_cache["valor"] = None
    _web_cache["t"] = 0.0
    _web_cache["valor"] = None


def _cargar_stock_inicial() -> None:
    """Una sola vez: si el stock propio está vacío, copia el de la tienda."""
    if _almacen is None or not _almacen.vacio():
        return
    try:
        n = _almacen.cargar(admin.filas_iniciales(_traer_productos()))
        log.info("Stock inicial: %s talles copiados de Tiendanube.", n)
        _invalidar_cache()
    except Exception:  # noqa: BLE001
        log.exception("No se pudo copiar el stock inicial de Tiendanube.")


def _revisar_modelos_nuevos() -> None:
    """Avisa por mail si en la tienda aparecieron modelos que el mayorista no conoce.

    La primera vez no avisa: lo que ya hay se toma como conocido. Después, solo los que aparecen.
    """
    if _almacen is None or not tn.configurado():
        return
    productos = _traer_productos()
    estado = _almacen.config("avisos_estado") or {}
    nuevos = admin.modelos_nuevos(productos, _almacen.incluidos(), {i["producto_id"] for i in _almacen.ignorados()})
    if "nuevosAvisados" in estado:
        sin_avisar = [n["nombre"] for n in nuevos if n["id"] not in set(estado["nuevosAvisados"])]
        if sin_avisar:
            _avisador.modelos_nuevos(sin_avisar)
    _almacen.guardar_config("avisos_estado", {**estado, "nuevosAvisados": [n["id"] for n in nuevos]})


async def _vigilar_la_tienda() -> None:
    """Cada 3 horas mira si hay modelos nuevos en la tienda (el primer vistazo, a los 2 minutos de arrancar)."""
    await asyncio.sleep(120)
    while True:
        try:
            await asyncio.to_thread(_revisar_modelos_nuevos)
        except Exception:  # noqa: BLE001
            log.exception("No se pudo revisar si hay modelos nuevos en la tienda")
        await asyncio.sleep(3 * 3600)


@asynccontextmanager
async def _vida(_app: FastAPI):
    # Crean las tablas; si no hay base, el contenedor falla a la vista.
    if _historial:
        _historial.preparar()
    vigilante = None
    if _almacen:
        _almacen.preparar()
        if CARGAR_STOCK_INICIAL:
            _cargar_stock_inicial()
        vigilante = asyncio.create_task(_vigilar_la_tienda())
    yield
    if vigilante:
        vigilante.cancel()


app = FastAPI(title="Mayorista", docs_url=None, redoc_url=None, lifespan=_vida)
app.include_router(admin.crear_router(_historial, _invalidar_cache, _almacen, CATEGORIAS, _ajustes_de_siempre, _agente, _avisador))


@app.middleware("http")
async def _cabeceras_del_panel(request: Request, llamar):
    """El panel no se guarda en cachés, no se mete en un marco y no se indexa.

    Y quien entra por www. pasa al dominio sin www, para que haya una sola dirección.
    """
    host = request.headers.get("host", "").split(":")[0].lower()
    if host.startswith("www.") and request.url.path != "/salud":
        destino = f"https://{host[4:]}{request.url.path}"
        if request.url.query:
            destino += f"?{request.url.query}"
        return RedirectResponse(destino, status_code=301)
    respuesta = await llamar(request)
    if request.url.path.startswith("/admin"):
        respuesta.headers["Cache-Control"] = "no-store"
        respuesta.headers["X-Frame-Options"] = "DENY"
        respuesta.headers["Referrer-Policy"] = "no-referrer"
        respuesta.headers["X-Robots-Tag"] = "noindex, nofollow"
    return respuesta


@app.get("/salud")
def salud() -> dict:
    return {"estado": "ok"}


@app.get("/api/catalogo")
def api_catalogo(response: Response) -> dict:
    try:
        catalogo = _catalogo_actual()
    except Exception:  # noqa: BLE001
        log.exception("No se pudo armar el catálogo")
        raise HTTPException(503, "No pudimos cargar el catálogo. Probá de nuevo en un minuto.") from None
    response.headers["Cache-Control"] = "no-store"
    w = web_vigente()
    return {
        "productos": catalogo,
        # Con los precios ocultos no se mandan: la web pide que los confirme una persona.
        "tramos": [] if w["ocultarPrecios"] else sorted(w["tramos"], key=lambda t: t["desde"], reverse=True),
        "minimo": w["minimo"],
        # Las líneas llevan el mínimo de cada una; con los precios ocultos van sin escalas.
        "lineas": [{**l, "tramos": [] if w["ocultarPrecios"] else sorted(l["tramos"], key=lambda t: t["desde"], reverse=True)} for l in w["lineas"]],
        "limitarStock": w["limitarStock"],
        "mostrarUnidades": w["mostrarUnidades"],
        "whatsapp": w["whatsapp"],
        "pedirEnvio": w["pedirEnvio"],
        "pago": w["pago"],
        "marca": w["marca"],
        "tiendaUrl": w["tiendaUrl"] if w["tiendaUrl"].lower().startswith(("http://", "https://")) else "",
        "categorias": [{"id": c["id"], "nombre": c["nombre"]} for c in categorias_vigentes()],
        # False: la web no muestra el filtro "Versión jugador / fan" (se deduce del
        # nombre) y usa solo las categorías de la tienda.
        "filtroVersion": w["filtroVersion"],
        "reglas": w["reglas"],
        "condiciones": w["condiciones"],
        "cierre": w["cierre"],
        "pie": w["pie"],
        "bandaIzq": w["bandaIzq"],
        "bandaDer": w["bandaDer"],
        "titulo": w["titulo"],
        "bajada": w["bajada"],
        "color": w["color"],
        "mensaje": w["mensaje"],
        "pausa": w["pausa"],
        "aviso": w["aviso"],
        "horario": w["horario"],
        "instagram": w["instagram"],
        "seoTitulo": w["seoTitulo"],
        "logo": "logo.webp" + (f"?v={w['logoHash']}" if w["logoHash"] else ""),
    }


@app.get("/api/marca")
def api_marca() -> dict:
    """Solo el nombre: lo usa el panel de administración."""
    return {"marca": web_vigente()["marca"]}


# -- Pedidos ------------------------------------------------------------------

PEDIDOS_POR_HORA = 20
_intentos: dict[str, list[float]] = {}


class LineaPedido(BaseModel):
    producto: int
    talle: str = Field(min_length=1, max_length=40)
    cantidad: int = Field(ge=1, le=999)
    pers: bool = False  # con parche y número (otra línea de precios)


class Envio(BaseModel):
    """Los datos para despachar el pedido (todos opcionales: la web los pide si el negocio lo configuró)."""

    dni: str = Field(default="", max_length=20)
    provincia: str = Field(default="", max_length=60)
    ciudad: str = Field(default="", max_length=80)
    cp: str = Field(default="", max_length=12)
    direccion: str = Field(default="", max_length=140)
    email: str = Field(default="", max_length=120)
    transporte: str = Field(default="", max_length=40)


class PedidoNuevo(BaseModel):
    codigo: str = Field(pattern=r"^A-[A-Z0-9]{5}$")
    items: list[LineaPedido] = Field(min_length=1, max_length=120)
    nombre: str = Field(default="", max_length=80)
    negocio: str = Field(default="", max_length=120)
    notas: str = Field(default="", max_length=600)
    envio: Envio = Field(default_factory=Envio)


def _ip_de(request: Request) -> str:
    return (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "")).split(",")[0].strip()


def _demasiados_pedidos(ip: str) -> bool:
    """Un freno simple para que nadie llene la base de pedidos falsos."""
    ahora = time.time()
    recientes = [t for t in _intentos.get(ip, []) if ahora - t < 3600]
    if len(recientes) >= PEDIDOS_POR_HORA:
        _intentos[ip] = recientes
        return True
    recientes.append(ahora)
    _intentos[ip] = recientes
    return False


def _total_estimado(items: list[dict], unidades: int) -> int | None:
    """El total del pedido: por línea si el negocio las tiene; si no, con la escala general."""
    lineas = web_vigente()["lineas"]
    if not lineas:
        precio = precio_unidad(unidades)
        return precio * unidades if precio else None
    cats = {p["id"]: categorias_de(p) for p in _cache["productos"] or []}
    return precios.total_del_pedido([(cats.get(i["producto"], []), i["cantidad"], bool(i.get("pers"))) for i in items], lineas, precio_unidad)


@app.post("/api/pedido")
def api_pedido(datos: PedidoNuevo, request: Request) -> dict:
    """Guarda el pedido. La web lo manda justo antes de abrir WhatsApp; si falla, el pedido sale igual."""
    if _almacen is None:
        raise HTTPException(503, "Los pedidos no se están guardando en este servidor.")
    if web_vigente()["pausa"]:
        raise HTTPException(409, "Los pedidos están en pausa por ahora.")
    if _demasiados_pedidos(_ip_de(request)):
        raise HTTPException(429, "Demasiados pedidos seguidos. Probá de nuevo más tarde.")

    nombres = {p["id"]: nombre_limpio((p.get("name") or {}).get("es") or "") for p in _cache["productos"] or []}
    items = [
        {"producto": i.producto, "talle": almacen.limpiar_talle(i.talle), "cantidad": i.cantidad, "nombre": nombres.get(i.producto) or f"Modelo {i.producto}", **({"pers": True} if i.pers else {})}
        for i in datos.items
    ]
    unidades = sum(i["cantidad"] for i in items)
    total = _total_estimado(items, unidades)
    guardado = _almacen.crear_pedido(
        datos.codigo,
        {"items": items, "nombre": datos.nombre.strip(), "negocio": datos.negocio.strip(), "notas": datos.notas.strip(),
         **({"envio": {k: v.strip() for k, v in datos.envio.model_dump().items() if v.strip()}} if any(v.strip() for v in datos.envio.model_dump().values()) else {})},
        unidades,
        total,
    )
    if guardado:
        _avisador.pedido_nuevo(datos.codigo, unidades, total, datos.nombre.strip(), datos.negocio.strip())
    # Mandar dos veces el mismo código (un doble toque) no duplica nada.
    return {"ok": True, "codigo": datos.codigo, "nuevo": guardado}


@app.get("/img/{producto}/{n}")
def imagen(producto: int, n: int) -> Response:
    try:
        _actualizar_si_hace_falta()
    except Exception:  # noqa: BLE001
        raise HTTPException(503, "Sin catálogo") from None
    urls = _cache["imagenes"].get(producto) or []
    if n < 1 or n > len(urls):
        raise HTTPException(404, "No existe esa foto")
    url = urls[n - 1]
    try:
        contenido = tn.bajar_foto(url)
    except Exception as e:  # noqa: BLE001
        log.warning("No se pudo bajar la foto %s: %s", url, e)
        raise HTTPException(502, "No se pudo cargar la foto") from None
    return Response(contenido, media_type="image/webp", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/logo.webp")
def logo() -> Response:
    """El logo que se subió desde el panel o, si no hay, el de la carpeta static."""
    try:
        guardado = _almacen.config("logo") if _almacen is not None else None
    except Exception:  # noqa: BLE001
        guardado = None
    if guardado and guardado.get("datos"):
        return Response(base64.b64decode(guardado["datos"]), media_type=guardado.get("tipo") or "image/png", headers={"Cache-Control": "public, max-age=300"})
    return FileResponse(CARPETA / "static" / "logo.webp", headers={"Cache-Control": "public, max-age=300"})


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
def pagina() -> HTMLResponse:
    """La página, con el título y la descripción que se eligieron en el panel (los buscadores no corren el JavaScript)."""
    texto = (CARPETA / "static" / "index.html").read_text(encoding="utf-8")
    w = web_vigente()
    if w["seoTitulo"].strip():
        texto = re.sub(r"<title>.*?</title>", lambda _: f"<title>{html.escape(w['seoTitulo'].strip())}</title>", texto, count=1, flags=re.S)
    if w["seoDescripcion"].strip():
        texto = re.sub(r'(<meta name="description" content=")[^"]*(")', lambda m: m.group(1) + html.escape(w["seoDescripcion"].strip(), quote=True) + m.group(2), texto, count=1)
    return HTMLResponse(texto, headers={"Cache-Control": "no-cache"})


# El resto de los archivos, al final: lo de arriba tiene prioridad.
app.mount("/", StaticFiles(directory=CARPETA / "static", html=True), name="web")


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(app, host="0.0.0.0", port=PUERTO, log_level="info")
