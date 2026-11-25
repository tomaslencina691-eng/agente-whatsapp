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
import avisos as avisos_mod
import historial as hist
import mp
import pagos as cruce_de_pagos
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


class CambiarClave(BaseModel):
    actual: str = Field(max_length=200)
    nueva: str = Field(max_length=200)


class NuevaPersona(BaseModel):
    usuario: str = Field(pattern=r"^[A-Za-z0-9._-]{3,30}$")
    clave: str = Field(max_length=200)
    rol: Literal["todo", "stock_web", "stock"]


class CambiosDePersona(BaseModel):
    rol: Literal["todo", "stock_web", "stock"] | None = None
    activo: bool | None = None
    clave: str | None = Field(default=None, max_length=200)


class ConfigDeAvisos(BaseModel):
    pedidoNuevo: bool = True
    pocasUnidades: bool = True
    umbral: int = Field(default=2, ge=0, le=100)
    modelosNuevos: bool = True
    fallasDeLaTienda: bool = True


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


class AsignarPago(BaseModel):
    codigo: str = Field(pattern=r"^A-[A-Z0-9]{5}$")


class ConfigDePagos(BaseModel):
    activo: bool = False
    modo: Literal["sugerir", "auto"] = "sugerir"


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


# -- El alta de un producto nuevo ------------------------------------------------

MAXIMO_DE_FOTOS = 8
PESO_MAXIMO_DE_FOTO = 6 * 1024 * 1024  # ya decodificada; el navegador la achica antes de mandarla
_TIPOS_DE_FOTO = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


class TalleNuevo(BaseModel):
    talle: str = Field(min_length=1, max_length=40)
    stock: int = Field(ge=0, le=MAXIMO_DE_STOCK)
    sku: str = Field(default="", max_length=60)


class FotoNueva(BaseModel):
    datos: str = Field(max_length=9_000_000)  # "data:image/jpeg;base64,..."


class ProductoNuevo(BaseModel):
    nombre: str = Field(min_length=3, max_length=200)
    descripcion: str = Field(default="", max_length=20000)
    categorias: list[int] = Field(default_factory=list, max_length=20)
    precio: float = Field(gt=0, le=100_000_000)
    precioPromocional: float | None = Field(default=None, gt=0, le=100_000_000)
    peso: float | None = Field(default=None, ge=0, le=1000)  # kg
    alto: float | None = Field(default=None, ge=0, le=1000)  # cm
    ancho: float | None = Field(default=None, ge=0, le=1000)
    profundidad: float | None = Field(default=None, ge=0, le=1000)
    marca: str = Field(default="", max_length=100)
    etiquetas: str = Field(default="", max_length=500)
    publicado: bool = False
    envioGratis: bool = False
    talles: list[TalleNuevo] = Field(min_length=1, max_length=30)
    fotos: list[FotoNueva] = Field(min_length=1, max_length=MAXIMO_DE_FOTOS)


def foto_en_base64(datos: str) -> tuple[str, str]:
    """Valida una foto que manda el navegador. Devuelve (base64 sin prefijo, extensión)."""
    m = re.fullmatch(r"data:(image/[a-z]+);base64,([A-Za-z0-9+/=\s]+)", (datos or "").strip())
    if not m or m.group(1) not in _TIPOS_DE_FOTO:
        raise ValueError("Una de las fotos no es JPG, PNG ni WebP.")
    crudo = re.sub(r"\s", "", m.group(2))
    try:
        contenido = base64.b64decode(crudo, validate=True)
    except Exception:  # noqa: BLE001
        raise ValueError("Una de las fotos llegó dañada: volvé a elegirla.") from None
    if len(contenido) > PESO_MAXIMO_DE_FOTO:
        raise ValueError("Una de las fotos pesa demasiado (más de 6 MB).")
    return crudo, _TIPOS_DE_FOTO[m.group(1)]


def _numero(valor: float | None) -> str | None:
    return None if valor is None else f"{valor:.2f}"


def _descripcion_html(texto: str) -> str:
    """El texto plano del formulario, como párrafos de HTML (que es lo que guarda Tiendanube)."""
    import html

    parrafos = [p.strip() for p in re.split(r"\n\s*\n", texto or "") if p.strip()]
    return "".join("<p>" + html.escape(p).replace("\n", "<br>") + "</p>" for p in parrafos)


def cuerpo_para_tiendanube(datos: ProductoNuevo) -> dict:
    """El producto como lo pide la API de Tiendanube: una variante por talle, con su stock contado."""
    talles = [" ".join(t.talle.split()) for t in datos.talles]
    if len({almacen.normalizar(t) for t in talles}) != len(talles):
        raise ValueError("Hay dos talles iguales.")
    if datos.precioPromocional is not None and datos.precioPromocional >= datos.precio:
        raise ValueError("El precio promocional tiene que ser menor que el precio.")
    variantes = []
    for talle, t in zip(talles, datos.talles):
        v = {
            "values": [{"es": talle}],
            "price": _numero(datos.precio),
            "stock_management": True,
            "stock": t.stock,
        }
        if datos.precioPromocional is not None:
            v["promotional_price"] = _numero(datos.precioPromocional)
        if t.sku.strip():
            v["sku"] = t.sku.strip()
        for campo, valor in (("weight", datos.peso), ("height", datos.alto), ("width", datos.ancho), ("depth", datos.profundidad)):
            if valor is not None:
                v[campo] = _numero(valor)
        variantes.append(v)
    cuerpo = {
        "name": {"es": " ".join(datos.nombre.split())},
        "description": {"es": _descripcion_html(datos.descripcion)},
        "published": datos.publicado,
        "free_shipping": datos.envioGratis,
        "attributes": [{"es": "Talle"}],
        "variants": variantes,
    }
    if datos.categorias:
        cuerpo["categories"] = list(dict.fromkeys(datos.categorias))
    if datos.marca.strip():
        cuerpo["brand"] = datos.marca.strip()
    etiquetas = [e.strip() for e in datos.etiquetas.split(",") if e.strip()]
    if etiquetas:
        cuerpo["tags"] = ",".join(etiquetas)
    return cuerpo


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
        "enTienda": p is not None,
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
    "whatsapp": "WhatsApp", "whatsappAlterno": "WhatsApp alternativo", "pedirEnvio": "datos de envío", "pago": "cómo pagar", "aliasPago": "alias", "titularPago": "titular de la cuenta", "cuentaPago": "cuenta", "mensaje": "mensaje del pedido", "minimo": "pedido mínimo", "tramos": "precios", "lineas": "precios por línea", "topeTalle": "tope por talle", "hitos": "metas de la barra", "pausa": "pausa de pedidos", "mantenimiento": "página en construcción", "mensajeMantenimiento": "cartel de construcción",
    "aviso": "aviso", "limitarStock": "límite de stock", "filtroVersion": "filtro de versión", "mostrarUnidades": "unidades que quedan",
    "ocultarPrecios": "precios ocultos", "orden": "orden", "reglas": "reglas", "condiciones": "condiciones", "cierre": "cierre", "pie": "pie",
    "horario": "horario", "instagram": "Instagram", "tiendaUrl": "enlace a la tienda", "seoTitulo": "título en Google", "seoDescripcion": "descripción en Google",
}


def modelos_nuevos(productos: list[dict], incluidos: set[int], ignorados: set[int]) -> list[dict]:
    """Lo que la tienda tiene (publicado, con foto y con stock contado) y el mayorista no incluyó ni ignoró."""
    return [{"id": p["id"], "nombre": _nombre_de(p, p["id"])} for p in productos if es_elegible(p) and p["id"] not in incluidos and p["id"] not in ignorados]


def crear_router(
    historial,
    invalidar_cache: Callable[[], None],
    almacen_,
    categorias_base: list[dict] | None = None,
    web_defecto: Callable[[], dict] | None = None,
    agente_=None,
    avisador=None,
    mercadopago=None,
) -> APIRouter:
    router = APIRouter(prefix="/admin/api")
    mp_ = mercadopago or mp  # Mercado Pago (se reemplaza en las pruebas)
    cuenta_mp: dict = {"datos": None}  # de quién es el token, la primera vez que se pregunta
    nombres: dict[int, str] = {}  # los últimos nombres vistos, para el historial
    tienda: dict = {"t": 0.0, "productos": None}  # lo último que se leyó de la tienda

    def configurado() -> bool:
        return acceso.configurado() and historial is not None and almacen_ is not None and tn.configurado()

    def exigir_panel() -> None:
        if not configurado():
            raise HTTPException(503, "El panel no está configurado en el servidor.")

    def quien_es(request: Request) -> dict | None:
        """La persona de la sesión y qué puede hacer, según la base en este momento."""
        return acceso.identidad(almacen_, acceso.leer_sesion(request.cookies.get(acceso.COOKIE)))

    def usuario_actual(request: Request, _=Depends(exigir_panel)) -> str:
        yo = quien_es(request)
        if not yo:
            raise HTTPException(401, "Iniciá sesión.")
        # Los cambios exigen un header propio: un sitio ajeno no puede mandarlo
        # desde el navegador sin permiso, y así nadie guarda cosas por vos.
        if request.method not in ("GET", "HEAD") and request.headers.get("x-mayorista") != "1":
            raise HTTPException(403, "Pedido no permitido.")
        # Cada parte del panel pide su permiso (ver acceso.grupo_de): quitárselo a alguien rige ya.
        if acceso.grupo_de(request.url.path) not in yo["permisos"]:
            raise HTTPException(403, "No tenés permiso para hacer esto.")
        return yo["usuario"]

    def nombre(id: int) -> str:
        return nombres.get(id) or f"Modelo {id}"

    def avisar_poco_stock(antes: list[dict], modelos: set[int]) -> None:
        """Avisa por mail los modelos que, con este cambio, llegaron al umbral de unidades o menos."""
        if avisador is None:
            return
        try:
            cruzaron = avisos_mod.cruzaron_el_umbral(antes, almacen_.filas(modelos), avisador.config()["umbral"])
            if cruzaron:
                avisador.pocas_unidades([(nombre(p), n) for p, n in cruzaron])
        except Exception:  # noqa: BLE001
            log.exception("No se pudo revisar si quedaron pocas unidades")

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
        yo = quien_es(request)
        return {"configurado": True, "usuario": yo["usuario"] if yo else None, "rol": yo["rol"] if yo else None, "permisos": yo["permisos"] if yo else []}

    @router.post("/login")
    def login(datos: Login, request: Request, response: Response, _=Depends(exigir_panel)) -> dict:
        ip = acceso.ip_del_pedido(request.headers.get("x-forwarded-for"), request.client.host if request.client else "")
        if acceso.bloqueado(ip):
            raise HTTPException(429, "Demasiados intentos. Probá de nuevo en unos minutos.")
        persona = acceso.autenticar(almacen_, datos.usuario, datos.clave)
        if not persona:
            acceso.anotar_fallo(ip)
            raise HTTPException(401, "Usuario o clave incorrectos.")
        acceso.limpiar_fallos(ip)
        try:
            almacen_.anotar_ingreso(acceso.normalizar_usuario(persona["usuario"]), persona["rol"])
        except Exception:  # noqa: BLE001
            log.exception("No se pudo anotar el ingreso de %s", persona["usuario"])
        response.set_cookie(
            acceso.COOKIE,
            acceso.crear_sesion(persona["usuario"]),
            max_age=acceso.DURACION_SEGUNDOS,
            httponly=True,
            samesite="strict",
            secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https",
            path="/",
        )
        return {"usuario": persona["usuario"], "rol": persona["rol"], "permisos": acceso.ROLES[persona["rol"]]}

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
        except almacen.ErrorDeTienda as e:
            raise HTTPException(502, detail={"mensaje": str(e), "conflictos": []}) from None
        except almacen.Conflicto as e:
            detalle = str(e)
            # Los avisos con nombre propio (un talle que no existe en Tiendanube) se muestran tal cual.
            mensaje = f"No se guardó nada: {detalle}" if "Tiendanube" in detalle else "No se guardó nada: el stock cambió justo ahora. Recargá la planilla."
            raise HTTPException(409, detail={"mensaje": mensaje, "conflictos": []}) from None

        # A cada talle nuevo se le anota el id de su fila, para poder deshacerlo.
        ids_nuevos = iter(nuevos)
        for e in entradas:
            if e["accion"] == "talle_nuevo":
                e["variante_id"] = next(ids_nuevos)

        lote = hist.lote_nuevo()
        anotar(lote, usuario, entradas)
        avisar_poco_stock(filas, {c.producto for c in cambios})
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

    # -- Cobros de Mercado Pago (ver mp.py y pagos.py) --

    def _config_pagos() -> dict:
        c = almacen_.config("pagos") or {}
        return {"activo": bool(c.get("activo")) and mp_.configurado(), "modo": c.get("modo") if c.get("modo") in ("sugerir", "auto") else "sugerir"}

    def _vista_de_cobro(c: dict, cruce: dict | None) -> dict:
        d = c.get("datos") or {}
        return {
            "id": c["id"], "monto": c["monto"], "fecha": c["fecha"], "pagador": d.get("pagador") or {}, "tipo": d.get("tipo") or "",
            "pedidos": (cruce or {}).get("pedidos", []), "seguro": (cruce or {}).get("seguro"),
        }

    def _cobros_por_resolver() -> list[dict]:
        """Los cobros detectados que todavía no se asignaron a un pedido, con los pedidos que podrían ser."""
        if not _config_pagos()["activo"]:
            return []
        detectados = almacen_.pagos("detectado", 100)
        cruzados = cruce_de_pagos.cruzar(detectados, almacen_.pedidos("nuevo", 500))
        return [_vista_de_cobro(c, cruzados.get(c["id"])) for c in detectados]

    def _mi_id():
        if cuenta_mp["datos"] is None:
            cuenta_mp["datos"] = mp_.cuenta()
        return cuenta_mp["datos"].get("id")

    def _asignar(cobro_id: str, codigo: str, usuario: str) -> dict:
        """Confirma el pedido con este cobro: marca el cobro, descuenta el stock y, si algo sale mal, lo suelta."""
        cobro = next((c for c in almacen_.pagos(None, 1000) if c["id"] == cobro_id), None)
        if cobro is None:
            raise HTTPException(404, "No existe ese cobro.")
        pedido = almacen_.pedido(codigo)
        if pedido is None or pedido["estado"] != "nuevo":
            raise HTTPException(409, "Ese pedido ya no está por confirmar.")
        if not almacen_.marcar_pago(cobro_id, "asignado", codigo):
            raise HTTPException(409, "Ese cobro ya se asignó o se ignoró.")
        try:
            r = _confirmar(codigo, usuario)
        except BaseException:
            almacen_.marcar_pago(cobro_id, "detectado", None, desde=("asignado",))
            raise
        return {**r, "cobro": cobro_id, "monto": cobro["monto"], "total": pedido["total"]}

    def sincronizar_pagos(usuario: str = "Mercado Pago") -> dict:
        """Mira lo que entró a Mercado Pago, guarda lo nuevo y, en modo automático, confirma los cruces seguros.

        Lo llama una tarea cada pocos minutos (ver sitio.py) y el botón «Revisar ahora» del panel.
        """
        if not _config_pagos()["activo"]:
            return {"revisado": False, "nuevos": 0, "confirmados": 0}
        try:
            cobros = mp_.cobros_recientes(mi_id=_mi_id())
        except mp.ErrorDeMercadoPago as e:
            log.warning("No se pudo revisar Mercado Pago: %s", e)
            raise HTTPException(502, str(e)) from None
        nuevos = almacen_.guardar_pagos(cobros)
        confirmados = 0
        if _config_pagos()["modo"] == "auto":
            for c in _cobros_por_resolver():
                if c["seguro"]:
                    try:
                        _asignar(c["id"], c["seguro"], usuario)
                        confirmados += 1
                    except HTTPException as e:
                        log.warning("No se pudo confirmar solo el pedido %s con el cobro %s: %s", c["seguro"], c["id"], e.detail)
        return {"revisado": True, "nuevos": len(nuevos), "confirmados": confirmados}

    router.sincronizar_pagos = sincronizar_pagos  # lo usa la tarea periódica de sitio.py

    @router.get("/pagos")
    def pagos_ver(usuario: str = Depends(usuario_actual)) -> dict:
        cfg = _config_pagos()
        return {"configurado": mp_.configurado(), **cfg, "porResolver": _cobros_por_resolver(),
                "asignados": [{"id": c["id"], "monto": c["monto"], "fecha": c["fecha"], "pedido": c["pedido"]} for c in almacen_.pagos("asignado", 10)]}

    @router.post("/pagos/revisar")
    def pagos_revisar(usuario: str = Depends(usuario_actual)) -> dict:
        if not _config_pagos()["activo"]:
            raise HTTPException(409, "La verificación de pagos está apagada.")
        return sincronizar_pagos(usuario)

    @router.post("/pagos/{cobro}/asignar")
    def pagos_asignar(cobro: str, datos: AsignarPago, usuario: str = Depends(usuario_actual)) -> dict:
        return _asignar(cobro, datos.codigo, usuario)

    @router.post("/pagos/{cobro}/ignorar")
    def pagos_ignorar(cobro: str, usuario: str = Depends(usuario_actual)) -> dict:
        if not almacen_.marcar_pago(cobro, "ignorado"):
            raise HTTPException(409, "Ese cobro ya se asignó o se ignoró.")
        return {"ignorado": True}

    @router.put("/pagos/config")
    def pagos_config(datos: ConfigDePagos, usuario: str = Depends(usuario_actual)) -> dict:
        if datos.activo and not mp_.configurado():
            raise HTTPException(422, "Falta cargar MERCADOPAGO_ACCESS_TOKEN en el servidor.")
        almacen_.guardar_config("pagos", {"activo": datos.activo, "modo": datos.modo})
        anotar(hist.lote_nuevo(), usuario, [{"accion": "config", "producto_id": 0, "producto": "Verificación de pagos (Mercado Pago)", "talle": f"{'activa' if datos.activo else 'apagada'} · {datos.modo}", "antes": None, "despues": None}])
        return _config_pagos()

    @router.post("/pagos/probar")
    def pagos_probar(usuario: str = Depends(usuario_actual)) -> dict:
        """Para comprobar la conexión: de quién es el token y los últimos cobros que ve (no guarda nada)."""
        try:
            cuenta_mp["datos"] = None
            yo = mp_.cuenta()
            cuenta_mp["datos"] = yo
            recientes = mp_.cobros_recientes(dias=7, limite=10, mi_id=yo.get("id"))
        except mp.ErrorDeMercadoPago as e:
            raise HTTPException(502, str(e)) from None
        return {"cuenta": yo, "recientes": recientes}

    # -- Stock compartido con Tiendanube (ver stock_tn.py) --

    def _compartido() -> object:
        if not hasattr(almacen_, "fijar_activo"):
            raise HTTPException(503, "El stock compartido no está disponible en este servidor.")
        return almacen_

    @router.get("/stock-compartido")
    def compartido_ver(usuario: str = Depends(usuario_actual)) -> dict:
        a = _compartido()
        salida: dict = {"activo": a.activo()}
        try:
            salida["comparacion"] = a.comparar()
        except almacen.ErrorDeTienda as e:
            salida["error"] = str(e)
        return salida

    @router.post("/stock-compartido/activar")
    def compartido_activar(usuario: str = Depends(usuario_actual)) -> dict:
        """Prende el stock compartido. Antes comprueba que Tiendanube deja escribir (sin cambiar nada)."""
        a = _compartido()
        if a.activo():
            return {"activo": True, "yaEstaba": True}
        try:
            comparacion = a.comparar()
            a.probar_escritura()
        except almacen.ErrorDeTienda as e:
            raise HTTPException(
                422,
                f"No se activó: {e} Revisá que la app de Tiendanube tenga el permiso «Escribir productos» y que el token del servidor sea el nuevo.",
            ) from None
        a.fijar_activo(True)
        invalidar_cache()
        anotar(hist.lote_nuevo(), usuario, [{"accion": "config", "producto_id": 0, "producto": "Stock compartido con Tiendanube", "talle": "activado", "antes": comparacion["unidadesPropias"], "despues": comparacion["unidadesTienda"]}])
        return {"activo": True, "comparacion": comparacion}

    @router.post("/stock-compartido/desactivar")
    def compartido_desactivar(usuario: str = Depends(usuario_actual)) -> dict:
        """Vuelve al stock propio del mayorista (el último que tenía guardado)."""
        a = _compartido()
        a.fijar_activo(False)
        invalidar_cache()
        anotar(hist.lote_nuevo(), usuario, [{"accion": "config", "producto_id": 0, "producto": "Stock compartido con Tiendanube", "talle": "desactivado", "antes": None, "despues": None}])
        return {"activo": False}

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

    # -- Subir un producto nuevo a Tiendanube --

    def _olvidar_lo_leido() -> None:
        tienda["t"] = 0.0
        if callable(getattr(almacen_, "invalidar", None)):
            almacen_.invalidar()
        invalidar_cache()

    @router.get("/productos/categorias")
    def productos_categorias(usuario: str = Depends(usuario_actual)) -> dict:
        """Las categorías de la tienda, para elegir en el alta. Las subcategorías llevan el nombre de la de arriba."""
        try:
            lista = tn.categorias()
        except tn.ErrorDeTiendanube as e:
            raise HTTPException(502, str(e)) from None
        por_id = {c["id"]: c for c in lista}

        def ruta(c: dict, vueltas: int = 0) -> str:
            propio = (c.get("name") or {}).get("es") or f"Categoría {c['id']}"
            padre = por_id.get(c.get("parent"))
            return f"{ruta(padre, vueltas + 1)} › {propio}" if padre and vueltas < 5 else propio

        salida = [{"id": c["id"], "nombre": ruta(c)} for c in lista]
        salida.sort(key=lambda c: c["nombre"].lower())
        return {"categorias": salida}

    @router.post("/productos")
    def producto_nuevo(datos: ProductoNuevo, usuario: str = Depends(usuario_actual)) -> dict:
        """Crea el producto en Tiendanube (con sus fotos) y lo suma al mayorista. Todo o nada."""
        try:
            cuerpo = cuerpo_para_tiendanube(datos)
            fotos = [foto_en_base64(f.datos) for f in datos.fotos]
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        try:
            p = tn.crear_producto(cuerpo)
        except tn.ErrorDeTiendanube as e:
            raise HTTPException(502, f"Tiendanube no creó el producto: {e}") from None
        id = p["id"]
        for i, (b64, ext) in enumerate(fotos, start=1):
            try:
                tn.subir_foto(id, b64, f"foto-{i}.{ext}", i)
            except tn.ErrorDeTiendanube as e:
                # Sin todas sus fotos no queda: se borra, para no dejar un producto a medias en la tienda.
                try:
                    tn.borrar_producto(id)
                    queda = "No quedó nada creado: probá de nuevo."
                except tn.ErrorDeTiendanube:
                    log.exception("No se pudo borrar el producto %s después de fallar una foto", id)
                    queda = f"El producto quedó creado en Tiendanube sin todas sus fotos (id {id}): borralo o completalo desde Tiendanube."
                raise HTTPException(502, f"No se pudo subir la foto {i}: {e}. {queda}") from None
        modelo = _nombre_de(p, id)
        nombres[id] = modelo
        filas = filas_de_producto(p, True)
        almacen_.cargar(filas)
        almacen_.designorar(id)
        anotar(hist.lote_nuevo(), usuario, [{"accion": "creado", "producto_id": id, "producto": modelo, "talle": ", ".join(t for _, t, _ in filas), "antes": None, "despues": sum(n for _, _, n in filas)}])
        _olvidar_lo_leido()
        return {"id": id, "nombre": modelo, "publicado": bool(p.get("published")), "talles": len(filas), "unidades": sum(n for _, _, n in filas)}

    @router.post("/productos/{producto}/publicar")
    def producto_publicar(producto: int, usuario: str = Depends(usuario_actual)) -> dict:
        """Publica en la tienda online un modelo que estaba oculto: desde ahí se ve también en la web mayorista."""
        if producto not in almacen_.incluidos():
            raise HTTPException(404, "Ese modelo no está en el mayorista.")
        try:
            tn.publicar(producto, True)
        except tn.ErrorDeTiendanube as e:
            raise HTTPException(502, str(e)) from None
        anotar(hist.lote_nuevo(), usuario, [{"accion": "config", "producto_id": producto, "producto": nombre(producto), "talle": "publicado en la tienda online", "antes": None, "despues": None}])
        _olvidar_lo_leido()
        return {"publicado": True}

    @router.post("/productos/{producto}/quitar")
    def producto_quitar(producto: int, usuario: str = Depends(usuario_actual)) -> dict:
        """Saca de la planilla un modelo que ya no existe en Tiendanube (lo borraron allá)."""
        if producto not in almacen_.incluidos():
            raise HTTPException(404, "Ese modelo no está en el mayorista.")
        try:
            sigue = tn.productos_por_id([producto]).get(producto)
        except tn.ErrorDeTiendanube as e:
            raise HTTPException(502, str(e)) from None
        if sigue is not None:
            raise HTTPException(409, "Ese modelo sigue en Tiendanube: solo se pueden quitar los que se borraron de la tienda.")
        modelo = nombre(producto)
        talles = ", ".join(f["talle"] for f in almacen_.filas([producto]))
        almacen_.quitar_modelo(producto)
        anotar(hist.lote_nuevo(), usuario, [{"accion": "quitado", "producto_id": producto, "producto": modelo, "talle": talles, "antes": None, "despues": None}])
        _olvidar_lo_leido()
        return {"quitado": True}

    # -- La web mayorista --

    def _web_guardada() -> dict:
        return almacen_.config("web") or {}

    def _web_actual() -> dict:
        return ajustes.combinar(web_defecto() if web_defecto else {}, _web_guardada())

    @router.get("/web")
    def web_ver(usuario: str = Depends(usuario_actual)) -> dict:
        """Los ajustes de la web como están ahora (lo guardado sobre lo de siempre) y lo de siempre, para poder volver."""
        guardado = almacen_.config("logo") or {}
        categorias = [{"id": c["id"], "nombre": c["nombre"]} for c in _categorias_actuales()]
        return {"ajustes": _web_actual(), "porDefecto": web_defecto() if web_defecto else {}, "logo": bool(guardado.get("datos")), "categorias": categorias}

    @router.put("/web")
    def web_guardar(datos: ajustes.AjustesWeb, usuario: str = Depends(usuario_actual)) -> dict:
        malos = ajustes.problemas(datos)
        if malos:
            raise HTTPException(422, " ".join(malos))
        antes = _web_actual()
        nuevo = datos.model_dump()
        nuevo["tramos"] = sorted(nuevo["tramos"], key=lambda t: t["desde"])
        for linea in nuevo["lineas"]:
            linea["nombre"] = " ".join(linea["nombre"].split())
            linea["tramos"] = sorted(linea["tramos"], key=lambda t: t["desde"])
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
            # El de prompts/sistema.md: el panel ofrece traerlo cuando cambia en el repositorio.
            "promptRepo": (estado or {}).get("prompt_base", ""),
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

    # -- Mi cuenta, personas con acceso y avisos --

    def _persona_publica(u: dict) -> dict:
        return {"usuario": u["usuario"], "rol": u["rol"], "rolNombre": acceso.NOMBRES_DE_ROLES.get(u["rol"], u["rol"]), "activo": u["activo"], "creado": u["creado"], "ultimoIngreso": u["ultimo_ingreso"]}

    def _conexiones(estado: dict | None, error: str, es_duenio: bool) -> list[dict]:
        c = (estado or {}).get("conexiones") or {}
        tienda = tn.configurado()
        return [
            {"id": "tiendanube", "nombre": "Tiendanube", "estado": "ok" if tienda else "pendiente", "detalle": "Solo lectura: modelos, fotos y categorías." if tienda else "Faltan las claves de la tienda."},
            {"id": "agente", "nombre": "Agente de WhatsApp", "estado": "ok" if estado else "error", "detalle": f"{estado['proveedor']} · {estado['modelo']}" if estado else error},
            {"id": "chatwoot", "nombre": "Chatwoot (bandeja de chats)", "estado": "ok" if c.get("chatwoot") else ("pendiente" if estado else "error"), "detalle": "Conectado." if c.get("chatwoot") else "El agente no tiene los datos de Chatwoot."},
            {"id": "whatsapp", "nombre": "WhatsApp (Meta)", "estado": "ok" if c.get("whatsapp") else "pendiente",
             "detalle": "Datos de Meta cargados (visto azul y «escribiendo...»)." if c.get("whatsapp") else "Falta conectar el número en Meta: tarjeta, verificación del negocio y webhook."},
            {"id": "correo", "nombre": "Mail de avisos", "estado": "ok" if c.get("avisos") else "pendiente",
             "detalle": (f"Los avisos llegan a {c.get('avisos_para')}." if es_duenio else "Configurado.") if c.get("avisos") else "Falta configurar el mail del agente (Gmail) y el «Mail de avisos» de la pestaña Agente."},
        ]

    @router.get("/cuenta")
    def cuenta_ver(request: Request, usuario: str = Depends(usuario_actual)) -> dict:
        yo = quien_es(request)
        es_duenio = "administrar" in yo["permisos"]
        estado, error = _estado_del_agente()
        salida = {
            "usuario": yo["usuario"], "rol": yo["rol"], "rolNombre": acceso.NOMBRES_DE_ROLES[yo["rol"]], "permisos": yo["permisos"], "esDuenio": es_duenio,
            "conexiones": _conexiones(estado, error, es_duenio),
        }
        if es_duenio:
            salida["personas"] = [_persona_publica(u) for u in almacen_.usuarios() if u["rol"] in acceso.ROLES_ASIGNABLES]
            salida["roles"] = [{"id": r, "nombre": acceso.NOMBRES_DE_ROLES[r]} for r in acceso.ROLES_ASIGNABLES]
            salida["avisos"] = avisador.config() if avisador else dict(avisos_mod.DEFECTO)
            salida["avisosPara"] = ((estado or {}).get("conexiones") or {}).get("avisos_para", "")
        return salida

    @router.post("/cuenta/clave")
    def cuenta_cambiar_clave(datos: CambiarClave, request: Request, usuario: str = Depends(usuario_actual)) -> dict:
        yo = quien_es(request)
        if not acceso.autenticar(almacen_, yo["usuario"], datos.actual):
            raise HTTPException(422, "La clave actual no es correcta.")
        if datos.nueva == datos.actual:
            raise HTTPException(422, "La clave nueva es igual a la actual.")
        problema = acceso.problema_de_clave(datos.nueva, yo["usuario"])
        if problema:
            raise HTTPException(422, problema)
        nueva = acceso.hashear(datos.nueva)
        if yo["rol"] == "duenio":
            almacen_.fijar_clave_del_duenio(acceso.normalizar_usuario(yo["usuario"]), nueva)
        else:
            almacen_.cambiar_usuario(acceso.normalizar_usuario(yo["usuario"]), clave_hash=nueva)
        anotar(hist.lote_nuevo(), yo["usuario"], [{"accion": "config", "producto_id": 0, "producto": "Cuenta", "talle": "cambió su clave", "antes": None, "despues": 1}])
        return {"guardado": True}

    @router.post("/usuarios")
    def persona_crear(datos: NuevaPersona, usuario: str = Depends(usuario_actual)) -> dict:
        nombre_ = acceso.normalizar_usuario(datos.usuario)
        if nombre_ == acceso.normalizar_usuario(acceso.USUARIO):
            raise HTTPException(422, "Ese usuario está reservado.")
        problema = acceso.problema_de_clave(datos.clave, nombre_)
        if problema:
            raise HTTPException(422, problema)
        if not almacen_.crear_usuario(nombre_, acceso.hashear(datos.clave), datos.rol, usuario):
            raise HTTPException(409, "Ya hay una persona con ese usuario.")
        anotar(hist.lote_nuevo(), usuario, [{"accion": "config", "producto_id": 0, "producto": "Personas con acceso", "talle": f"alta de {nombre_} ({acceso.NOMBRES_DE_ROLES[datos.rol]})", "antes": None, "despues": 1}])
        return _persona_publica(almacen_.usuario(nombre_))

    def _persona_ajena(nombre_: str) -> dict:
        nombre_ = acceso.normalizar_usuario(nombre_)
        if nombre_ == acceso.normalizar_usuario(acceso.USUARIO):
            raise HTTPException(422, "El dueño no se modifica desde acá.")
        u = almacen_.usuario(nombre_)
        if u is None or u["rol"] not in acceso.ROLES_ASIGNABLES:
            raise HTTPException(404, "No existe esa persona.")
        return u

    @router.put("/usuarios/{nombre_}")
    def persona_cambiar(nombre_: str, datos: CambiosDePersona, usuario: str = Depends(usuario_actual)) -> dict:
        u = _persona_ajena(nombre_)
        campos: dict = {}
        cambios: list[str] = []
        if datos.rol is not None and datos.rol != u["rol"]:
            campos["rol"] = datos.rol
            cambios.append(f"rol: {acceso.NOMBRES_DE_ROLES[datos.rol]}")
        if datos.activo is not None and datos.activo != u["activo"]:
            campos["activo"] = datos.activo
            cambios.append("activada" if datos.activo else "desactivada")
        if datos.clave:
            problema = acceso.problema_de_clave(datos.clave, u["usuario"])
            if problema:
                raise HTTPException(422, problema)
            campos["clave_hash"] = acceso.hashear(datos.clave)
            cambios.append("clave nueva")
        if campos:
            almacen_.cambiar_usuario(u["usuario"], **campos)
            anotar(hist.lote_nuevo(), usuario, [{"accion": "config", "producto_id": 0, "producto": "Personas con acceso", "talle": f"{u['usuario']}: " + ", ".join(cambios), "antes": None, "despues": len(cambios)}])
        return _persona_publica(almacen_.usuario(u["usuario"]))

    @router.delete("/usuarios/{nombre_}")
    def persona_quitar(nombre_: str, usuario: str = Depends(usuario_actual)) -> dict:
        u = _persona_ajena(nombre_)
        almacen_.borrar_usuario(u["usuario"])
        anotar(hist.lote_nuevo(), usuario, [{"accion": "config", "producto_id": 0, "producto": "Personas con acceso", "talle": f"baja de {u['usuario']}", "antes": None, "despues": 0}])
        return {"quitada": True}

    @router.get("/avisos")
    def avisos_ver(usuario: str = Depends(usuario_actual)) -> dict:
        return avisador.config() if avisador else dict(avisos_mod.DEFECTO)

    @router.put("/avisos")
    def avisos_guardar(datos: ConfigDeAvisos, usuario: str = Depends(usuario_actual)) -> dict:
        antes = avisador.config() if avisador else dict(avisos_mod.DEFECTO)
        nuevo = datos.model_dump()
        almacen_.guardar_config("avisos", nuevo)
        if avisador:
            avisador.invalidar()
        etiquetas = {"pedidoNuevo": "pedido nuevo", "pocasUnidades": "pocas unidades", "umbral": "umbral", "modelosNuevos": "modelos nuevos", "fallasDeLaTienda": "fallas de la tienda"}
        cambiados = [etiquetas[k] for k in etiquetas if antes.get(k) != nuevo.get(k)]
        if cambiados:
            anotar(hist.lote_nuevo(), usuario, [{"accion": "config", "producto_id": 0, "producto": "Avisos por mail", "talle": ", ".join(cambiados), "antes": None, "despues": len(cambiados)}])
        return {"guardado": True, "cambios": cambiados}

    @router.post("/avisos/probar")
    def avisos_probar(usuario: str = Depends(usuario_actual)) -> dict:
        """Manda un mail de prueba al «Mail de avisos» del agente y dice si salió (o por qué no)."""
        if agente_ is None:
            raise HTTPException(503, "El panel no está conectado al agente.")
        try:
            return agente_.mail_de_prueba()
        except agente_cliente.ErrorDelAgente as e:
            raise HTTPException(502, str(e)) from None

    # -- Pedidos --

    def _vista_de_pedido(p: dict, stock_actual: dict[tuple[int, str], int], cobro: dict | None = None) -> dict:
        datos = p["datos"]
        items = [
            {"producto": i["producto"], "nombre": i.get("nombre") or nombre(i["producto"]), "talle": i["talle"], "cantidad": i["cantidad"], "pers": bool(i.get("pers")),
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
            "envio": datos.get("envio") or {}, "items": items, "falta": falta, "pagoDetectado": cobro,
            "desparejo": datos.get("tallesDesparejos") or [],
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
        cobros = _cobros_por_resolver()
        seguros = {c["seguro"]: c for c in cobros if c["seguro"]}
        return {
            "pedidos": [_vista_de_pedido(p, stock_actual, seguros.get(p["codigo"])) for p in lista],
            "cuenta": almacen_.cuenta_por_estado(),
            "whatsapp": _web_actual().get("whatsapp", ""),
            "pagos": {"activo": _config_pagos()["activo"], "porResolver": [c for c in cobros if not c["seguro"]]},
        }

    def _movimientos(codigo: str, accion: str, movs: list[dict]) -> list[dict]:
        pedido = almacen_.pedido(codigo)
        por_producto = {i["producto"]: i.get("nombre") for i in (pedido or {}).get("datos", {}).get("items", [])}
        return [
            {"accion": accion, "producto_id": m["producto_id"], "variante_id": m["id"], "producto": f"Pedido {codigo} · {por_producto.get(m['producto_id']) or nombre(m['producto_id'])}",
             "talle": m["talle"], "antes": m["antes"], "despues": m["despues"]}
            for m in movs
        ]

    def _error_de_pedido(e: almacen.ErrorDeAlmacen) -> HTTPException:
        if isinstance(e, almacen.ErrorDeTienda):
            return HTTPException(502, detail={"mensaje": str(e), "conflictos": []})
        if isinstance(e, almacen.PedidoNoExiste):
            return HTTPException(404, str(e))
        if isinstance(e, almacen.FaltaStock):
            conflictos = [{"modelo": nombre(f["producto"]), "talle": f["talle"], "motivo": f"Pidió {f['pidio']} y hay {f['hay']}."} for f in e.faltantes]
            return HTTPException(409, detail={"mensaje": "No alcanza el stock para confirmar este pedido. No se descontó nada.", "conflictos": conflictos})
        return HTTPException(409, detail={"mensaje": str(e), "conflictos": []})

    @router.post("/pedidos/{codigo}/confirmar")
    def pedido_confirmar(codigo: str, usuario: str = Depends(usuario_actual)) -> dict:
        return _confirmar(codigo, usuario)

    def _confirmar(codigo: str, usuario: str) -> dict:
        previo = almacen_.pedido(codigo)
        modelos = {i["producto"] for i in previo["datos"]["items"]} if previo else set()
        antes = almacen_.filas(modelos) if modelos else []
        try:
            movs = almacen_.confirmar(codigo)
        except almacen.ErrorDeAlmacen as e:
            raise _error_de_pedido(e) from None
        anotar(hist.lote_nuevo(), usuario, _movimientos(codigo, "pedido", movs))
        avisar_poco_stock(antes, modelos)
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
