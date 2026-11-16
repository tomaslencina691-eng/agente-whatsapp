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

import logging
import time
from datetime import datetime, timezone
from typing import Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

import acceso
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


def crear_router(historial, invalidar_cache: Callable[[], None], almacen_, categorias_base: list[dict] | None = None) -> APIRouter:
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
