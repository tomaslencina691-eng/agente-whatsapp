"""El panel de administración del mayorista: stock como una planilla y pedidos.

Todo cuelga de /admin/api y exige la sesión de `acceso`. El stock que se edita
es el PROPIO del mayorista (ver almacen.py): no se mezcla con el de la tienda.
Tiendanube solo aporta los nombres y las fotos, y se usa para traer los modelos
nuevos que la persona subió a su tienda.

Cómo se protege un guardado:

  · Cada cambio viaja con el valor que la persona VIO ("antes"). Si en el medio
    entró un pedido confirmado y el stock ya no es ese, no se pisa: se avisa
    (409) y no se aplica nada. Todo o nada, para que un cambio de a varios no
    quede a medias (en la base es una sola transacción).
  · Cada guardado queda en el historial, y se puede deshacer entero.
  · Confirmar un pedido descuenta el stock; cancelarlo lo devuelve.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from datetime import datetime, timezone
from typing import Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

import acceso
import agente_cliente
import ajustes
import almacen
import historial as hist
import tn

log = logging.getLogger("mayorista.admin")

MAXIMO_DE_CAMBIOS = 500
MAXIMO_DE_STOCK = 9999

ORDEN_DE_TALLES = ["XS", "S", "M", "L", "XL", "XXL", "XXXL"]

# La primera foto de cada modelo, de la última vez que se listó la planilla.
_foto_de_producto: dict[int, str] = {}


# -- Lo que llega -------------------------------------------------------------


class Login(BaseModel):
    usuario: str = Field(max_length=100)
    clave: str = Field(max_length=200)


class Cambio(BaseModel):
    tipo: Literal["stock", "talle_nuevo"]
    producto: int
    variante: int | None = None
    talle: str | None = Field(default=None, max_length=40)
    antes: int | None = None
    despues: int = Field(ge=0, le=MAXIMO_DE_STOCK)


class Aplicar(BaseModel):
    cambios: list[Cambio] = Field(max_length=MAXIMO_DE_CAMBIOS)


class Incluir(BaseModel):
    producto: int
    copiar_stock: bool = False


class UnModelo(BaseModel):
    producto: int


class Categoria(BaseModel):
    """Un filtro de la web: su nombre y qué categorías de la tienda lo forman."""

    id: str = Field(min_length=1, max_length=30, pattern=r"^[a-z0-9_-]+$")
    nombre: str = Field(min_length=1, max_length=40)
    tiendanube: list[str] = Field(default=[], max_length=60)


class GuardarCategorias(BaseModel):
    categorias: list[Categoria] = Field(max_length=20)


# Los datos del negocio que el prompt del agente puede usar como {{alias_pago}}. Es la misma lista que
# tiene el agente (src/agente/panel_db.py): una prueba las compara.
VARIABLES_DEL_AGENTE = ("alias_pago", "horario", "costo_envio", "dias_despacho", "politica_cambios")
ETIQUETAS_DE_AJUSTES_DEL_AGENTE = {
    "activo": "agente activo", "mensajesPorRespuesta": "mensajes por respuesta", "topeMensajesPorDia": "tope por día",
    "respetarResueltas": "chats resueltos", "soloElPrimerMensaje": "solo el primer mensaje", "avisosEmail": "mail de avisos",
}


class PromptNuevo(BaseModel):
    texto: str = Field(min_length=20, max_length=30000)
    nota: str = Field(default="", max_length=120)


class Restaurar(BaseModel):
    id: int


class AjustesDelAgente(BaseModel):
    activo: bool = True
    mensajesPorRespuesta: int = Field(ge=1, le=5)
    topeMensajesPorDia: int = Field(ge=0, le=1000)
    respetarResueltas: bool = False
    soloElPrimerMensaje: bool = False
    avisosEmail: str = Field(default="", max_length=120, pattern=r"^(|[^@\s]+@[^@\s]+\.[^@\s]+)$")


class DatosDelNegocio(BaseModel):
    alias_pago: str = Field(default="", max_length=200)
    horario: str = Field(default="", max_length=200)
    costo_envio: str = Field(default="", max_length=200)
    dias_despacho: str = Field(default="", max_length=200)
    politica_cambios: str = Field(default="", max_length=300)


class MensajeDePrueba(BaseModel):
    rol: Literal["yo", "agente"]
    texto: str = Field(min_length=1, max_length=2000)


class ProbarAgente(BaseModel):
    prompt: str = Field(min_length=20, max_length=30000)
    mensajes: list[MensajeDePrueba] = Field(min_length=1, max_length=30)


class SubirLogo(BaseModel):
    datos: str = Field(max_length=2_000_000)  # la imagen en base64, con o sin el prefijo "data:image/...;base64,"


# -- Ayudantes ----------------------------------------------------------------


def talle_de(variante: dict) -> str:
    valores = variante.get("values") or []
    return ", ".join(v.get("es", "") for v in valores if v.get("es")) or "Único"


def _clave_de_talle(talle: str) -> tuple[int, str]:
    base = talle.split(" - ")[0].strip().upper()
    return (ORDEN_DE_TALLES.index(base) if base in ORDEN_DE_TALLES else 99, talle)


def _nombre_de(p: dict | None, id: int) -> str:
    nombre = ((p or {}).get("name") or {}).get("es")
    return nombre or (f"Modelo {id} (ya no está en la tienda)" if p is None else f"Modelo {id}")


def vista_de_producto(p: dict | None, id: int, filas: list[dict]) -> dict:
    """Un modelo para la planilla: los datos de la tienda y el stock propio."""
    variantes = [{"id": f["id"], "talle": f["talle"], "stock": f["cantidad"]} for f in filas]
    variantes.sort(key=lambda v: _clave_de_talle(v["talle"]))
    return {
        "id": id,
        "nombre": _nombre_de(p, id),
        "publicado": bool(p and p.get("published")),
        "categorias": [(c.get("name") or {}).get("es", "") for c in (p or {}).get("categories") or []],
        "foto": bool(p and p.get("images")),
        "variantes": variantes,
    }


def categorias_del_producto(producto: dict, categorias: list[dict]) -> list[str]:
    """Los ids de los filtros de la web a los que pertenece un modelo, según sus categorías en la tienda."""
    nombres = {(c.get("name") or {}).get("es") for c in producto.get("categories") or []}
    return [c["id"] for c in categorias if nombres & set(c.get("tiendanube") or [])]


def tiene_stock_contado(p: dict) -> bool:
    return any(v.get("stock") is not None for v in p.get("variants") or [])


def es_elegible(p: dict) -> bool:
    """Un modelo de la tienda que puede estar en el mayorista: publicado, con foto y con stock contado."""
    return bool(p.get("published") and p.get("images") and tiene_stock_contado(p))


def filas_de_producto(p: dict, copiar_stock: bool) -> list[tuple[int, str, int]]:
    """Las filas de stock de un modelo: un talle por variante, con el stock de la tienda o en 0."""
    filas: list[tuple[int, str, int]] = []
    vistos: set[str] = set()
    for v in p.get("variants") or []:
        if v.get("stock") is None:  # Tiendanube no cuenta esa variante: en mayorista hace falta saber cuántas hay
            continue
        talle = almacen.limpiar_talle(talle_de(v))
        if almacen.normalizar(talle) in vistos:
            continue
        vistos.add(almacen.normalizar(talle))
        filas.append((p["id"], talle, max(0, int(v["stock"])) if copiar_stock else 0))
    return filas


def filas_iniciales(productos: list[dict]) -> list[tuple[int, str, int]]:
    """Para la primera vez: todo lo que hay en la tienda, con su stock actual."""
    return [fila for p in productos if es_elegible(p) for fila in filas_de_producto(p, True)]


# -- El router ----------------------------------------------------------------


ETIQUETAS_DE_AJUSTES = {
    "marca": "nombre", "bandaIzq": "banda", "bandaDer": "banda", "titulo": "título", "bajada": "texto de portada", "color": "color",
    "whatsapp": "WhatsApp", "mensaje": "mensaje del pedido", "minimo": "pedido mínimo", "tramos": "precios", "pausa": "pausa de pedidos",
    "aviso": "aviso", "limitarStock": "límite de stock", "filtroVersion": "filtro de versión", "mostrarUnidades": "unidades que quedan",
    "ocultarPrecios": "precios ocultos", "orden": "orden", "reglas": "reglas", "condiciones": "condiciones", "cierre": "cierre", "pie": "pie",
    "horario": "horario", "instagram": "Instagram", "tiendaUrl": "enlace a la tienda", "seoTitulo": "título en Google", "seoDescripcion": "descripción en Google",
}


def crear_router(
    historial,
    invalidar_cache: Callable[[], None],
    almacen_,
    categorias_base: list[dict] | None = None,
    web_defecto: Callable[[], dict] | None = None,
    agente_=None,
) -> APIRouter:
    router = APIRouter(prefix="/admin/api")
    nombres: dict[int, str] = {}  # los últimos nombres vistos, para el historial
    tienda: dict = {"t": 0.0, "productos": None}  # lo último que se leyó de la tienda

    def configurado() -> bool:
        return acceso.configurado() and historial is not None and almacen_ is not None and tn.configurado()

    def exigir_panel() -> None:
        if not configurado():
            raise HTTPException(503, "El panel no está configurado en el servidor.")

    def usuario_actual(request: Request, _=Depends(exigir_panel)) -> str:
        usuario = acceso.leer_sesion(request.cookies.get(acceso.COOKIE))
        if not usuario:
            raise HTTPException(401, "Iniciá sesión.")
        # Los cambios exigen un header propio: un sitio ajeno no puede mandarlo
        # desde el navegador sin permiso, y así nadie guarda cosas por vos.
        if request.method not in ("GET", "HEAD") and request.headers.get("x-mayorista") != "1":
            raise HTTPException(403, "Pedido no permitido.")
        return usuario

    def nombre(id: int) -> str:
        return nombres.get(id) or f"Modelo {id}"

    def anotar(lote: str, usuario: str, entradas: list[dict]) -> None:
        """Lo que ya se aplicó no se deshace por un fallo del historial: se avisa en el log."""
        try:
            historial.anotar(lote, usuario, entradas)
        except Exception:  # noqa: BLE001
            log.exception("No se pudo anotar en el historial (lote %s)", lote)

    # -- Sesión --

    @router.get("/estado")
    def estado(request: Request) -> dict:
        """Para que la página sepa si mostrar el login, sin dar error."""
        if not configurado():
            return {"configurado": False, "usuario": None}
        return {"configurado": True, "usuario": acceso.leer_sesion(request.cookies.get(acceso.COOKIE))}

    @router.post("/login")
    def login(datos: Login, request: Request, response: Response, _=Depends(exigir_panel)) -> dict:
        ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "")).split(",")[0].strip()
        if acceso.bloqueado(ip):
            raise HTTPException(429, "Demasiados intentos. Probá de nuevo en unos minutos.")
        if not acceso.credenciales_validas(datos.usuario.strip(), datos.clave):
            acceso.anotar_fallo(ip)
            raise HTTPException(401, "Usuario o clave incorrectos.")
        acceso.limpiar_fallos(ip)
        response.set_cookie(
            acceso.COOKIE,
            acceso.crear_sesion(acceso.USUARIO),
            max_age=acceso.DURACION_SEGUNDOS,
            httponly=True,
            samesite="strict",
            secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https",
            path="/",
        )
        return {"usuario": acceso.USUARIO}

    @router.post("/logout")
    def logout(response: Response) -> dict:
        response.delete_cookie(acceso.COOKIE, path="/")
        return {"ok": True}

    # -- Stock --

    def _productos_de_la_tienda(guardados: bool = False) -> list[dict]:
        """Lo que hay en Tiendanube ahora. Con `guardados`, vale lo leído hace menos de 2 minutos."""
        if guardados and tienda["productos"] is not None and time.time() - tienda["t"] < 120:
            return tienda["productos"]
        try:
            productos = tn.todos_los_productos()
        except tn.ErrorDeTiendanube as e:
            raise HTTPException(502, str(e)) from None
        for p in productos:
            nombres[p["id"]] = _nombre_de(p, p["id"])
            imagenes = sorted(p.get("images") or [], key=lambda i: i.get("position") or 0)
            if imagenes:
                _foto_de_producto[p["id"]] = imagenes[0]["src"]
        tienda.update(t=time.time(), productos=productos)
        return productos

    def _categorias_actuales() -> list[dict]:
        guardadas = almacen_.config("categorias")
        return list(categorias_base or []) if guardadas is None else guardadas

    @router.get("/stock")
    def stock(usuario: str = Depends(usuario_actual)) -> dict:
        productos = {p["id"]: p for p in _productos_de_la_tienda()}
        por_modelo: dict[int, list[dict]] = {}
        for f in almacen_.filas():
            por_modelo.setdefault(f["producto_id"], []).append(f)
        vistas = [vista_de_producto(productos.get(id), id, filas) for id, filas in por_modelo.items()]
        vistas.sort(key=lambda v: v["nombre"].lower())
        return {"productos": vistas, "stock_maximo": MAXIMO_DE_STOCK, "vacio": not por_modelo}

    @router.get("/foto/{producto}")
    def foto(producto: int, usuario: str = Depends(usuario_actual)) -> Response:
        url = _foto_de_producto.get(producto)
        if not url:
            raise HTTPException(404, "Sin foto")
        try:
            contenido = tn.bajar_foto(tn.miniatura(url, 240))
        except OSError:
            raise HTTPException(502, "No se pudo cargar la foto") from None
        return Response(contenido, media_type="image/webp", headers={"Cache-Control": "private, max-age=3600"})

    # -- Guardar --

    def _aplicar(usuario: str, cambios: list[Cambio]) -> dict:
        """El corazón: valida contra el stock de AHORA y aplica todo o nada."""
        if not cambios:
            raise HTTPException(422, "No hay cambios para guardar.")
        filas = almacen_.filas({c.producto for c in cambios})
        por_id = {f["id"]: f for f in filas}

        conflictos: list[dict] = []
        stocks: list[tuple[int, int, int]] = []
        altas: list[tuple[int, str, int]] = []
        entradas: list[dict] = []

        for c in cambios:
            modelo = nombre(c.producto)
            del_modelo = [f for f in filas if f["producto_id"] == c.producto]

            if c.tipo == "stock":
                f = por_id.get(c.variante or 0)
                if f is None or f["producto_id"] != c.producto:
                    conflictos.append({"producto": c.producto, "modelo": modelo, "motivo": "Ese talle ya no existe."})
                    continue
                if f["cantidad"] != c.antes:
                    conflictos.append(
                        {"producto": c.producto, "modelo": modelo, "talle": f["talle"], "esperado": c.antes, "actual": f["cantidad"],
                         "motivo": "El stock cambió mientras editabas."}
                    )
                    continue
                if c.despues == f["cantidad"]:
                    continue
                stocks.append((f["id"], f["cantidad"], c.despues))
                entradas.append({"accion": "stock", "producto_id": c.producto, "variante_id": f["id"], "producto": modelo, "talle": f["talle"], "antes": f["cantidad"], "despues": c.despues})
            else:
                talle = almacen.limpiar_talle(c.talle or "")
                if not talle:
                    conflictos.append({"producto": c.producto, "modelo": modelo, "motivo": "Falta el talle."})
                    continue
                if not del_modelo:
                    conflictos.append({"producto": c.producto, "modelo": modelo, "talle": talle, "motivo": "Ese modelo no está en el mayorista."})
                    continue
                if any(almacen.normalizar(f["talle"]) == almacen.normalizar(talle) for f in del_modelo):
                    conflictos.append({"producto": c.producto, "modelo": modelo, "talle": talle, "motivo": "Ese talle ya existe."})
                    continue
                if c.despues == 0:
                    continue
                altas.append((c.producto, talle, c.despues))
                entradas.append({"accion": "talle_nuevo", "producto_id": c.producto, "producto": modelo, "talle": talle, "antes": None, "despues": c.despues})

        if conflictos:
            raise HTTPException(409, detail={"mensaje": "No se guardó nada: algo cambió desde que abriste la planilla.", "conflictos": conflictos})
        if not stocks and not altas:
            return {"lote": None, "aplicados": 0, "creados": 0}

        try:
            nuevos = almacen_.aplicar(stocks, altas)
        except almacen.Conflicto:
            raise HTTPException(409, detail={"mensaje": "No se guardó nada: el stock cambió justo ahora. Recargá la planilla.", "conflictos": []}) from None

        # A cada talle nuevo se le anota el id de su fila, para poder deshacerlo.
        ids_nuevos = iter(nuevos)
        for e in entradas:
            if e["accion"] == "talle_nuevo":
                e["variante_id"] = next(ids_nuevos)

        lote = hist.lote_nuevo()
        anotar(lote, usuario, entradas)
        return {"lote": lote, "aplicados": len(stocks), "creados": len(altas)}

    @router.post("/stock/aplicar")
    def aplicar(datos: Aplicar, usuario: str = Depends(usuario_actual)) -> dict:
        return _aplicar(usuario, datos.cambios)

    # -- Historial --

    @router.get("/historial")
    def ver_historial(usuario: str = Depends(usuario_actual)) -> list[dict]:
        return historial.lotes(30)

    @router.post("/historial/{lote}/deshacer")
    def deshacer(lote: str, usuario: str = Depends(usuario_actual)) -> dict:
        entradas = [e for e in historial.entradas_del_lote(lote) if e["accion"] in hist.REVERSIBLES and not e["deshecho"]]
        if not entradas:
            raise HTTPException(404, "No hay nada para deshacer en ese guardado.")
        # Las altas de talle se deshacen dejando el talle en 0: el talle queda, vacío.
        cambios = [
            Cambio(tipo="stock", producto=e["producto_id"], variante=e["variante_id"], antes=e["despues"], despues=e["antes"] or 0)
            for e in entradas
        ]
        resultado = _aplicar(usuario, cambios)
        historial.marcar_deshecho(lote)
        return resultado

    # -- Modelos de la tienda --

    @router.get("/tienda/nuevos")
    def tienda_nuevos(refrescar: bool = False, usuario: str = Depends(usuario_actual)) -> dict:
        """Lo que la tienda tiene y el mayorista no, y por qué algunos modelos no pueden entrar."""
        productos = _productos_de_la_tienda(guardados=not refrescar)
        incluidos = almacen_.incluidos()
        ignorados = {i["producto_id"]: i for i in almacen_.ignorados()}
        categorias = _categorias_actuales()
        nuevos: list[dict] = []
        no_entran: dict[str, list[dict]] = {"sin_foto": [], "ocultos": [], "sin_control": [], "sin_categoria": []}
        for p in productos:
            ref = {"id": p["id"], "nombre": _nombre_de(p, p["id"])}
            if not p.get("published"):
                if p["id"] not in incluidos and p["id"] not in ignorados:
                    no_entran["ocultos"].append(ref)
                continue
            if not p.get("images"):
                if p["id"] not in incluidos and p["id"] not in ignorados:
                    no_entran["sin_foto"].append(ref)
                continue
            if not tiene_stock_contado(p):
                if p["id"] not in incluidos and p["id"] not in ignorados:
                    no_entran["sin_control"].append(ref)
                continue
            if categorias and not categorias_del_producto(p, categorias) and p["id"] not in ignorados:
                no_entran["sin_categoria"].append(ref)  # entra, pero solo se ve en "Todas"
            if p["id"] in incluidos or p["id"] in ignorados:
                continue
            talles = [{"talle": t, "stock": n} for _, t, n in filas_de_producto(p, True)]
            nuevos.append({**ref, "foto": True, "talles": talles, "unidades": sum(t["stock"] for t in talles)})
        nuevos.sort(key=lambda n: n["nombre"].lower())
        return {
            "nuevos": nuevos,
            "incluidos": len(incluidos),
            "vacio": not incluidos,
            "ignorados": [{"id": i["producto_id"], "nombre": i["nombre"]} for i in ignorados.values()],
            "no_entran": {k: sorted(v, key=lambda r: r["nombre"].lower()) for k, v in no_entran.items()},
            "revisado": datetime.fromtimestamp(tienda["t"], timezone.utc).isoformat(),
        }

    @router.post("/tienda/ignorar")
    def tienda_ignorar(datos: UnModelo, usuario: str = Depends(usuario_actual)) -> dict:
        """Un modelo de la tienda que no se quiere ofrecer: deja de aparecer como nuevo."""
        if datos.producto in almacen_.incluidos():
            raise HTTPException(409, "Ese modelo ya está en el mayorista: no se puede ignorar.")
        modelo = nombre(datos.producto)
        almacen_.ignorar(datos.producto, modelo)
        anotar(hist.lote_nuevo(), usuario, [{"accion": "ignorado", "producto_id": datos.producto, "producto": modelo, "talle": None, "antes": None, "despues": None}])
        return {"ignorado": True, "nombre": modelo}

    @router.post("/tienda/restaurar")
    def tienda_restaurar(datos: UnModelo, usuario: str = Depends(usuario_actual)) -> dict:
        """Vuelve a considerar un modelo ignorado: reaparece entre los nuevos."""
        almacen_.designorar(datos.producto)
        anotar(hist.lote_nuevo(), usuario, [{"accion": "restaurado", "producto_id": datos.producto, "producto": nombre(datos.producto), "talle": None, "antes": None, "despues": None}])
        return {"restaurado": True}

    @router.get("/tienda/categorias")
    def tienda_categorias(usuario: str = Depends(usuario_actual)) -> dict:
        """Los filtros de la web y las categorías de la tienda que se les pueden asignar."""
        try:
            de_la_tienda = tn.categorias()
        except tn.ErrorDeTiendanube as e:
            raise HTTPException(502, str(e)) from None
        productos = _productos_de_la_tienda(guardados=True)
        modelos: dict[str, int] = {}
        for p in productos:
            if not p.get("published"):
                continue
            for n in {(c.get("name") or {}).get("es") for c in p.get("categories") or []} - {None, ""}:
                modelos[n] = modelos.get(n, 0) + 1
        nombres_tn = sorted({(c.get("name") or {}).get("es", "") for c in de_la_tienda} - {""}, key=str.lower)
        return {"categorias": _categorias_actuales(), "disponibles": [{"nombre": n, "modelos": modelos.get(n, 0)} for n in nombres_tn]}

    @router.put("/tienda/categorias")
    def tienda_guardar_categorias(datos: GuardarCategorias, usuario: str = Depends(usuario_actual)) -> dict:
        ids = [c.id for c in datos.categorias]
        nombres_c = [c.nombre.strip().lower() for c in datos.categorias]
        if len(set(ids)) != len(ids) or len(set(nombres_c)) != len(nombres_c):
            raise HTTPException(422, "Hay dos filtros con el mismo nombre.")
        if any(not c.nombre.strip() for c in datos.categorias):
            raise HTTPException(422, "Todos los filtros necesitan un nombre.")
        limpias = [{"id": c.id, "nombre": " ".join(c.nombre.split()), "tiendanube": list(dict.fromkeys(n.strip() for n in c.tiendanube if n.strip()))} for c in datos.categorias]
        almacen_.guardar_config("categorias", limpias)
        anotar(hist.lote_nuevo(), usuario, [{"accion": "config", "producto_id": 0, "producto": "Filtros de la web", "talle": ", ".join(c["nombre"] for c in limpias)[:200], "antes": None, "despues": len(limpias)}])
        invalidar_cache()
        return {"guardadas": len(limpias)}

    @router.post("/tienda/incluir")
    def tienda_incluir(datos: Incluir, usuario: str = Depends(usuario_actual)) -> dict:
        try:
            p = tn.productos_por_id([datos.producto]).get(datos.producto)
        except tn.ErrorDeTiendanube as e:
            raise HTTPException(502, str(e)) from None
        if p is None or not es_elegible(p):
            raise HTTPException(422, "Ese modelo no se puede incluir: tiene que estar publicado y con foto.")
        if datos.producto in almacen_.incluidos():
            raise HTTPException(409, "Ese modelo ya está en el mayorista.")
        filas = filas_de_producto(p, datos.copiar_stock)
        almacen_.cargar(filas)
        almacen_.designorar(p["id"])
        modelo = _nombre_de(p, p["id"])
        nombres[p["id"]] = modelo
        anotar(hist.lote_nuevo(), usuario, [{"accion": "incluido", "producto_id": p["id"], "producto": modelo, "talle": ", ".join(t for _, t, _ in filas), "antes": None, "despues": sum(n for _, _, n in filas)}])
        invalidar_cache()
        return {"incluido": True, "talles": len(filas), "nombre": modelo}

    @router.post("/tienda/cargar-stock")
    def tienda_cargar_stock(usuario: str = Depends(usuario_actual)) -> dict:
        """La primera vez: copia a la base propia todo lo que hay en la tienda, con su stock."""
        if not almacen_.vacio():
            raise HTTPException(409, "El mayorista ya tiene stock cargado: esto es solo para la primera vez.")
        filas = filas_iniciales(_productos_de_la_tienda())
        if not filas:
            raise HTTPException(422, "No hay modelos publicados con foto y stock en la tienda.")
        n = almacen_.cargar(filas)
        anotar(hist.lote_nuevo(), usuario, [{"accion": "carga_inicial", "producto_id": 0, "producto": "Carga inicial desde la tienda", "talle": f"{len({f[0] for f in filas})} modelos", "antes": None, "despues": sum(f[2] for f in filas)}])
        invalidar_cache()
        return {"talles": n, "modelos": len({f[0] for f in filas})}

    # -- La web mayorista --

    def _web_guardada() -> dict:
        return almacen_.config("web") or {}

    def _web_actual() -> dict:
        return ajustes.combinar(web_defecto() if web_defecto else {}, _web_guardada())

    @router.get("/web")
    def web_ver(usuario: str = Depends(usuario_actual)) -> dict:
        """Los ajustes de la web como están ahora (lo guardado sobre lo de siempre) y lo de siempre, para poder volver."""
        guardado = almacen_.config("logo") or {}
        return {"ajustes": _web_actual(), "porDefecto": web_defecto() if web_defecto else {}, "logo": bool(guardado.get("datos"))}

    @router.put("/web")
    def web_guardar(datos: ajustes.AjustesWeb, usuario: str = Depends(usuario_actual)) -> dict:
        malos = ajustes.problemas(datos)
        if malos:
            raise HTTPException(422, " ".join(malos))
        antes = _web_actual()
        nuevo = datos.model_dump()
        nuevo["tramos"] = sorted(nuevo["tramos"], key=lambda t: t["desde"])
        nuevo["reglas"] = [{k: v.strip() for k, v in r.items()} for r in nuevo["reglas"] if any(v.strip() for v in r.values())]
        for campo in ("marca", "bandaIzq", "bandaDer", "titulo", "bajada", "aviso", "condiciones", "cierre", "pie", "horario", "instagram", "tiendaUrl", "seoTitulo", "seoDescripcion"):
            nuevo[campo] = " ".join(nuevo[campo].split())
        nuevo["color"] = nuevo["color"].lower()
        nuevo["mensaje"] = nuevo["mensaje"].strip()
        nuevo["logoHash"] = antes.get("logoHash", "")
        almacen_.guardar_config("web", nuevo)
        cambiados = list(dict.fromkeys(ETIQUETAS_DE_AJUSTES[k] for k in ETIQUETAS_DE_AJUSTES if antes.get(k) != nuevo.get(k)))
        if cambiados:
            anotar(hist.lote_nuevo(), usuario, [{"accion": "config", "producto_id": 0, "producto": "Web mayorista", "talle": ", ".join(cambiados)[:200], "antes": None, "despues": len(cambiados)}])
        invalidar_cache()
        return {"guardado": True, "cambios": cambiados}

    @router.put("/web/logo")
    def web_subir_logo(datos: SubirLogo, usuario: str = Depends(usuario_actual)) -> dict:
        try:
            crudo, tipo = ajustes.leer_logo(datos.datos)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        huella = ajustes.huella(crudo)
        almacen_.guardar_config("logo", {"tipo": tipo, "datos": base64.b64encode(crudo).decode(), "hash": huella})
        almacen_.guardar_config("web", {**_web_guardada(), "logoHash": huella})
        anotar(hist.lote_nuevo(), usuario, [{"accion": "config", "producto_id": 0, "producto": "Web mayorista", "talle": "logo", "antes": None, "despues": 1}])
        invalidar_cache()
        return {"logo": True, "version": huella}

    @router.delete("/web/logo")
    def web_quitar_logo(usuario: str = Depends(usuario_actual)) -> dict:
        """Vuelve al logo de la carpeta del sitio."""
        almacen_.guardar_config("logo", {})
        almacen_.guardar_config("web", {**_web_guardada(), "logoHash": ""})
        anotar(hist.lote_nuevo(), usuario, [{"accion": "config", "producto_id": 0, "producto": "Web mayorista", "talle": "logo quitado", "antes": None, "despues": 0}])
        invalidar_cache()
        return {"logo": False}

    # -- El agente de WhatsApp --

    def _variables_usadas(texto: str) -> list[str]:
        return list(dict.fromkeys(re.findall(r"\{\{(\w+)\}\}", texto)))

    def _datos_guardados() -> dict:
        guardados = almacen_.config("agente_datos") or {}
        return {k: str(guardados.get(k) or "") for k in VARIABLES_DEL_AGENTE}

    def _estado_del_agente() -> tuple[dict | None, str]:
        """Lo que dice el agente de sí mismo (modelo, prompt del repositorio, ajustes de entorno) o por qué no contesta."""
        if agente_ is None:
            return None, "El panel no está conectado al agente (falta AGENTE_URL o AGENTE_PANEL_TOKEN)."
        try:
            return agente_.estado(), ""
        except agente_cliente.ErrorDelAgente as e:
            return None, str(e)

    @router.get("/agente")
    def agente_ver(usuario: str = Depends(usuario_actual)) -> dict:
        """Todo lo que muestra la pestaña del agente."""
        estado, error = _estado_del_agente()
        activo = almacen_.prompt_activo()
        sin_publicar = activo is None
        prompt = activo or ({"id": None, "texto": estado["prompt_base"], "usuario": "", "nota": "Todavía no se publicó ninguna versión: es la del repositorio.", "fecha": None, "activa": False} if estado else None)
        base = (estado or {}).get("base") or {}
        guardados = almacen_.config("agente_ajustes") or {}
        ajustes_ = {**base, **{k: v for k, v in guardados.items() if k in ETIQUETAS_DE_AJUSTES_DEL_AGENTE}} if base else {**guardados}
        return {
            "agente": {"conectado": estado is not None, "error": error, "modelo": (estado or {}).get("modelo", ""), "proveedor": (estado or {}).get("proveedor", "")},
            "prompt": prompt,
            "sinPublicar": sin_publicar,
            "versiones": almacen_.prompt_versiones(),
            "datos": _datos_guardados(),
            "variables": list(VARIABLES_DEL_AGENTE),
            "ajustes": ajustes_,
            "ajustesBase": base,
        }

    @router.get("/agente/versiones/{id}")
    def agente_version(id: int, usuario: str = Depends(usuario_actual)) -> dict:
        v = almacen_.prompt_version(id)
        if v is None:
            raise HTTPException(404, "No existe esa versión.")
        return v

    def _publicar(usuario: str, texto: str, nota: str) -> dict:
        texto = texto.strip()
        desconocidas = [v for v in _variables_usadas(texto) if v not in VARIABLES_DEL_AGENTE]
        if desconocidas:
            raise HTTPException(422, "El prompt usa datos que no existen: " + ", ".join("{{" + v + "}}" for v in desconocidas) + ".")
        activo = almacen_.prompt_activo()
        if activo and activo["texto"].strip() == texto:
            raise HTTPException(422, "No hay cambios para publicar: es igual a la versión que está en uso.")
        nuevo = almacen_.publicar_prompt(texto, usuario, nota.strip())
        datos = _datos_guardados()
        vacias = [v for v in _variables_usadas(texto) if not datos.get(v, "").strip()]
        anotar(hist.lote_nuevo(), usuario, [{"accion": "config", "producto_id": 0, "producto": "Prompt del agente", "talle": f"versión {nuevo}" + (f": {nota.strip()}" if nota.strip() else ""), "antes": None, "despues": nuevo}])
        return {"id": nuevo, "vacias": vacias}

    @router.put("/agente/prompt")
    def agente_publicar(datos: PromptNuevo, usuario: str = Depends(usuario_actual)) -> dict:
        """Publica una versión nueva del prompt: el agente la usa en menos de medio minuto."""
        return _publicar(usuario, datos.texto, datos.nota)

    @router.post("/agente/prompt/restaurar")
    def agente_restaurar(datos: Restaurar, usuario: str = Depends(usuario_actual)) -> dict:
        """Vuelve a una versión anterior: se publica como una versión nueva, así el historial no se pierde."""
        v = almacen_.prompt_version(datos.id)
        if v is None:
            raise HTTPException(404, "No existe esa versión.")
        return _publicar(usuario, v["texto"], f"Restaurada de la versión {datos.id}")

    @router.put("/agente/ajustes")
    def agente_guardar_ajustes(datos: AjustesDelAgente, usuario: str = Depends(usuario_actual)) -> dict:
        # "Antes" es lo que regía: lo del entorno del agente con lo guardado encima.
        estado, _ = _estado_del_agente()
        antes = {**((estado or {}).get("base") or {}), **(almacen_.config("agente_ajustes") or {})}
        nuevo = {**datos.model_dump(), "avisosEmail": datos.avisosEmail.strip()}
        almacen_.guardar_config("agente_ajustes", nuevo)
        cambiados = [ETIQUETAS_DE_AJUSTES_DEL_AGENTE[k] for k in ETIQUETAS_DE_AJUSTES_DEL_AGENTE if antes.get(k) != nuevo.get(k)]
        if cambiados:
            anotar(hist.lote_nuevo(), usuario, [{"accion": "config", "producto_id": 0, "producto": "Ajustes del agente", "talle": ", ".join(cambiados)[:200], "antes": None, "despues": len(cambiados)}])
        return {"guardado": True, "cambios": cambiados}

    @router.put("/agente/datos")
    def agente_guardar_datos(datos: DatosDelNegocio, usuario: str = Depends(usuario_actual)) -> dict:
        antes = _datos_guardados()
        nuevo = {k: " ".join(v.split()) for k, v in datos.model_dump().items()}
        almacen_.guardar_config("agente_datos", nuevo)
        cambiados = [k for k in VARIABLES_DEL_AGENTE if antes.get(k) != nuevo.get(k)]
        if cambiados:
            anotar(hist.lote_nuevo(), usuario, [{"accion": "config", "producto_id": 0, "producto": "Datos del negocio (agente)", "talle": ", ".join(cambiados), "antes": None, "despues": len(cambiados)}])
        return {"guardado": True, "cambios": cambiados}

    @router.post("/agente/probar")
    def agente_probar(datos: ProbarAgente, usuario: str = Depends(usuario_actual)) -> dict:
        """Una prueba con el prompt del editor (sin publicar), el stock y los datos de verdad."""
        if datos.mensajes[-1].rol != "yo":
            raise HTTPException(422, "El último mensaje tiene que ser de la persona.")
        if agente_ is None:
            raise HTTPException(503, "El panel no está conectado al agente.")
        try:
            return agente_.probar(datos.prompt, [m.model_dump() for m in datos.mensajes], _datos_guardados())
        except agente_cliente.ErrorDelAgente as e:
            raise HTTPException(502, str(e)) from None

    # -- Pedidos --

    def _vista_de_pedido(p: dict, stock_actual: dict[tuple[int, str], int]) -> dict:
        datos = p["datos"]
        items = [
            {"producto": i["producto"], "nombre": i.get("nombre") or nombre(i["producto"]), "talle": i["talle"], "cantidad": i["cantidad"],
             "hay": stock_actual.get((i["producto"], almacen.normalizar(i["talle"])), 0)}
            for i in datos["items"]
        ]
        # Lo mismo que hace confirmar: junta las líneas repetidas antes de comparar.
        pedido_por_talle: dict[tuple[int, str], int] = {}
        for i in items:
            k = (i["producto"], almacen.normalizar(i["talle"]))
            pedido_por_talle[k] = pedido_por_talle.get(k, 0) + i["cantidad"]
        falta = p["estado"] == "nuevo" and any(stock_actual.get(k, 0) < n for k, n in pedido_por_talle.items())
        return {
            "codigo": p["codigo"], "fecha": p["fecha"], "estado": p["estado"], "unidades": p["unidades"], "total": p["total"],
            "nombre": datos.get("nombre", ""), "negocio": datos.get("negocio", ""), "notas": datos.get("notas", ""),
            "items": items, "falta": falta,
        }

    @router.get("/pedidos")
    def pedidos(estado: str = "nuevo", usuario: str = Depends(usuario_actual)) -> dict:
        if estado != "todos" and estado not in almacen.ESTADOS:
            raise HTTPException(422, "Estado desconocido.")
        lista = almacen_.pedidos(None if estado == "todos" else estado)
        productos = {i["producto"] for p in lista for i in p["datos"]["items"]}
        stock_actual = {(f["producto_id"], almacen.normalizar(f["talle"])): f["cantidad"] for f in almacen_.filas(productos)} if productos else {}
        # Los nombres que guardó la web sirven para el historial aunque la planilla no se haya abierto.
        for p in lista:
            for i in p["datos"]["items"]:
                nombres.setdefault(i["producto"], i.get("nombre") or "")
        return {"pedidos": [_vista_de_pedido(p, stock_actual) for p in lista], "cuenta": almacen_.cuenta_por_estado()}

    def _movimientos(codigo: str, accion: str, movs: list[dict]) -> list[dict]:
        pedido = almacen_.pedido(codigo)
        por_producto = {i["producto"]: i.get("nombre") for i in (pedido or {}).get("datos", {}).get("items", [])}
        return [
            {"accion": accion, "producto_id": m["producto_id"], "variante_id": m["id"], "producto": f"Pedido {codigo} · {por_producto.get(m['producto_id']) or nombre(m['producto_id'])}",
             "talle": m["talle"], "antes": m["antes"], "despues": m["despues"]}
            for m in movs
        ]

    def _error_de_pedido(e: almacen.ErrorDeAlmacen) -> HTTPException:
        if isinstance(e, almacen.PedidoNoExiste):
            return HTTPException(404, str(e))
        if isinstance(e, almacen.FaltaStock):
            conflictos = [{"modelo": nombre(f["producto"]), "talle": f["talle"], "motivo": f"Pidió {f['pidio']} y hay {f['hay']}."} for f in e.faltantes]
            return HTTPException(409, detail={"mensaje": "No alcanza el stock para confirmar este pedido. No se descontó nada.", "conflictos": conflictos})
        return HTTPException(409, detail={"mensaje": str(e), "conflictos": []})

    @router.post("/pedidos/{codigo}/confirmar")
    def pedido_confirmar(codigo: str, usuario: str = Depends(usuario_actual)) -> dict:
        try:
            movs = almacen_.confirmar(codigo)
        except almacen.ErrorDeAlmacen as e:
            raise _error_de_pedido(e) from None
        anotar(hist.lote_nuevo(), usuario, _movimientos(codigo, "pedido", movs))
        return {"codigo": codigo, "estado": "confirmado", "unidades": sum(m["antes"] - m["despues"] for m in movs)}

    @router.post("/pedidos/{codigo}/cancelar")
    def pedido_cancelar(codigo: str, usuario: str = Depends(usuario_actual)) -> dict:
        try:
            anterior, movs = almacen_.cancelar(codigo)
        except almacen.ErrorDeAlmacen as e:
            raise _error_de_pedido(e) from None
        if movs:
            anotar(hist.lote_nuevo(), usuario, _movimientos(codigo, "pedido_cancelado", movs))
        return {"codigo": codigo, "estado": "cancelado", "devuelto": anterior == "confirmado"}

    @router.post("/pedidos/{codigo}/despachar")
    def pedido_despachar(codigo: str, usuario: str = Depends(usuario_actual)) -> dict:
        try:
            almacen_.despachar(codigo)
        except almacen.ErrorDeAlmacen as e:
            raise _error_de_pedido(e) from None
        return {"codigo": codigo, "estado": "despachado"}

    return router
