"""El stock compartido con Tiendanube.

Con el «stock compartido» activado (se prende desde el panel, en Cuenta), hay UN
solo stock: el de Tiendanube.

  · El mayorista lo LEE de Tiendanube (la web, el panel y el agente muestran lo
    mismo que la tienda online).
  · Todo lo que cambia el stock desde el mayorista se ESCRIBE en Tiendanube:
    confirmar un pedido (pago verificado) descuenta, cancelar uno confirmado
    devuelve, y editar la planilla o cargar por curva lo fija. Una venta de la
    tienda online ya descuenta del mismo stock, así que se ve en el mayorista.

Apagado, nada de esto rige: el mayorista sigue con su stock propio (almacen.py).

Esta clase envuelve al almacén de siempre: lo que no es stock (pedidos, personas,
ajustes, modelos ignorados) lo sigue resolviendo el almacén. Qué modelos y talles
ofrece el mayorista lo sigue diciendo su tabla de stock; las cantidades salen de
Tiendanube, y si en Tiendanube se agrega un talle a un modelo que el mayorista
ya ofrece, el talle aparece solo.

Cómo se evita vender de más:
  · Confirmar y cancelar «reclaman» el pedido en un solo paso (nuevo → confirmado)
    antes de tocar Tiendanube: dos personas no pueden confirmar el mismo pedido.
  · Antes de descontar se mira el stock FRESCO de Tiendanube (no el guardado).
  · El descuento es relativo («variation»): no pisa una venta que entró en el medio.
  · Si algo falla a mitad, se devuelve lo que ya se había descontado y el pedido
    vuelve a su estado.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import almacen
import tn as tn_real

log = logging.getLogger("mayorista.stock_tn")

# Cuánto se guarda lo leído de Tiendanube (segundos): la web y el panel no la consultan en cada visita.
SEGUNDOS_DE_LA_TIENDA = int(os.getenv("MAYORISTA_STOCK_SEGUNDOS", "30"))
SEGUNDOS_DEL_INTERRUPTOR = 10


def _talle_de(variante: dict) -> str:
    valores = variante.get("values") or []
    return almacen.limpiar_talle(", ".join(v.get("es", "") for v in valores if v.get("es")) or "Único")


def _stock_de(variante: dict | None) -> int | None:
    """El stock de una variante; None si Tiendanube no lo cuenta (stock infinito o sin control)."""
    if variante is None or variante.get("stock") is None:
        return None
    return max(0, int(variante["stock"]))


class AlmacenConTiendanube:
    def __init__(self, base, tienda=None) -> None:
        self._base = base
        self._tn = tienda or tn_real
        self._candado = threading.RLock()
        self._productos: dict[int, dict] = {}
        self._t = 0.0
        self._interruptor = (0.0, False)

    def __getattr__(self, nombre: str):
        # Todo lo que no es stock (pedidos, personas, ajustes...) lo hace el almacén de siempre.
        return getattr(self._base, nombre)

    # -- El interruptor ---------------------------------------------------------

    def activo(self) -> bool:
        t, valor = self._interruptor
        if time.time() - t < SEGUNDOS_DEL_INTERRUPTOR:
            return valor
        try:
            valor = bool((self._base.config("stock") or {}).get("enTiendanube"))
        except Exception:  # noqa: BLE001
            log.warning("No se pudo leer si el stock compartido está activo: se usa el último valor conocido.")
            return valor
        self._interruptor = (time.time(), valor)
        return valor

    def fijar_activo(self, valor: bool) -> None:
        self._base.guardar_config("stock", {"enTiendanube": bool(valor)})
        self._interruptor = (time.time(), bool(valor))
        self.invalidar()

    def invalidar(self) -> None:
        self._t = 0.0

    # -- Leer Tiendanube --------------------------------------------------------

    def _todos(self, forzar: bool = False) -> dict[int, dict]:
        with self._candado:
            if not forzar and self._productos and time.time() - self._t < SEGUNDOS_DE_LA_TIENDA:
                return self._productos
            try:
                lista = self._tn.todos_los_productos()
            except self._tn.ErrorDeTiendanube as e:
                if self._productos and not forzar:
                    log.warning("No se pudo leer Tiendanube: se muestra lo último que se leyó (%s)", e)
                    return self._productos
                raise almacen.ErrorDeTienda(f"No se pudo leer el stock de Tiendanube: {e}") from None
            self._productos = {p["id"]: p for p in lista}
            self._t = time.time()
            return self._productos

    def _frescos(self, ids: set[int]) -> dict[int, dict]:
        """Los modelos tal como están ahora en Tiendanube (sin usar lo guardado)."""
        try:
            lista = self._tn.productos_por_id(sorted(ids))
        except self._tn.ErrorDeTiendanube as e:
            raise almacen.ErrorDeTienda(f"No se pudo leer el stock de Tiendanube: {e}") from None
        with self._candado:
            self._productos.update(lista)
        return lista

    @staticmethod
    def _variante(producto: dict | None, talle: str) -> dict | None:
        buscado = almacen.normalizar(talle)
        for v in (producto or {}).get("variants") or []:
            if almacen.normalizar(_talle_de(v)) == buscado:
                return v
        return None

    def _cantidad(self, producto: dict | None, talle: str) -> int:
        return _stock_de(self._variante(producto, talle)) or 0

    def _ids_de(self, modelos: set[int]) -> dict[tuple[int, str], int]:
        return {(f["producto_id"], almacen.normalizar(f["talle"])): f["id"] for f in self._base.filas(modelos)}

    # -- Stock ------------------------------------------------------------------

    def filas(self, productos=None) -> list[dict]:
        filas = self._base.filas(productos)
        if not self.activo():
            return filas
        try:
            todos = self._todos()
        except almacen.ErrorDeTienda as e:
            log.warning("%s: se usa el stock propio por ahora.", e)
            return filas
        filas = self._sumar_talles_nuevos(filas, todos, productos)
        return [{**f, "cantidad": self._cantidad(todos.get(f["producto_id"]), f["talle"])} for f in filas]

    def _sumar_talles_nuevos(self, filas: list[dict], todos: dict[int, dict], productos) -> list[dict]:
        """Un talle que se agregó en Tiendanube a un modelo que el mayorista ya ofrece, entra solo."""
        por_modelo: dict[int, set[str]] = {}
        for f in filas:
            por_modelo.setdefault(f["producto_id"], set()).add(almacen.normalizar(f["talle"]))
        nuevas: list[tuple[int, str, int]] = []
        for modelo, talles in por_modelo.items():
            for v in (todos.get(modelo) or {}).get("variants") or []:
                talle = _talle_de(v)
                if _stock_de(v) is not None and almacen.normalizar(talle) not in talles:
                    talles.add(almacen.normalizar(talle))
                    nuevas.append((modelo, talle, 0))
        if not nuevas:
            return filas
        self._base.cargar(nuevas)
        return self._base.filas(productos)

    def aplicar(self, cambios: list[tuple[int, int, int]], altas: list[tuple[int, str, int]]) -> list[int]:
        """Lo mismo que el almacén, pero el stock se cambia en Tiendanube. Todo o nada."""
        if not self.activo():
            return self._base.aplicar(cambios, altas)

        base = {f["id"]: f for f in self._base.filas()}
        modelos = {base[fid]["producto_id"] for fid, _, _ in cambios if fid in base} | {p for p, _, _ in altas}
        frescos = self._frescos(modelos) if modelos else {}

        plan: list[tuple[int, int, int, int]] = []  # (modelo, variante, stock que hay, stock nuevo)
        for fid, antes, despues in cambios:
            f = base.get(fid)
            if f is None:
                raise almacen.Conflicto("Ese talle ya no existe.")
            v = self._variante(frescos.get(f["producto_id"]), f["talle"])
            if v is None or _stock_de(v) is None:
                raise almacen.Conflicto(f"El talle {f['talle']} ya no existe en Tiendanube o no lleva control de stock.")
            if _stock_de(v) != antes:
                raise almacen.Conflicto("El stock cambió mientras se guardaba.")
            plan.append((f["producto_id"], v["id"], antes, despues))
        for modelo, talle, cantidad in altas:
            v = self._variante(frescos.get(modelo), talle)
            if v is None or _stock_de(v) is None:
                raise almacen.Conflicto(f"El talle {talle} no existe en Tiendanube: creálo ahí primero y después cargale el stock.")
            if _stock_de(v) != cantidad:
                plan.append((modelo, v["id"], _stock_de(v) or 0, cantidad))

        hechos: list[tuple[int, int, int]] = []  # (modelo, variante, stock anterior)
        try:
            for modelo, variante, antes, despues in plan:
                self._tn.fijar_stock(modelo, variante, despues)
                hechos.append((modelo, variante, antes))
        except self._tn.ErrorDeTiendanube as e:
            fallas = self._devolver_fijando(hechos)
            raise almacen.ErrorDeTienda(self._mensaje_de_falla("No se guardó el stock", e, fallas)) from None
        self.invalidar()
        # Los talles nuevos quedan anotados en la base propia (con su id), que es lo que el historial usa.
        return self._base.aplicar([], altas) if altas else []

    def _devolver_fijando(self, hechos: list[tuple[int, int, int]]) -> list[str]:
        fallas: list[str] = []
        for modelo, variante, antes in reversed(hechos):
            try:
                self._tn.fijar_stock(modelo, variante, antes)
            except Exception as e:  # noqa: BLE001
                log.error("No se pudo devolver el stock del modelo %s variante %s a %s: %s", modelo, variante, antes, e)
                fallas.append(f"modelo {modelo} (variante {variante}) debería tener {antes}")
        return fallas

    def _devolver_ajustando(self, hechos: list[tuple[int, int, int]]) -> list[str]:
        """Deshace ajustes relativos: cada uno de `hechos` es (modelo, variante, lo que se sumó)."""
        fallas: list[str] = []
        for modelo, variante, cambio in reversed(hechos):
            try:
                self._tn.ajustar_stock(modelo, variante, -cambio)
            except Exception as e:  # noqa: BLE001
                log.error("No se pudo deshacer el ajuste de %s del modelo %s variante %s: %s", cambio, modelo, variante, e)
                fallas.append(f"modelo {modelo} (variante {variante}) quedó {cambio:+d} de lo que correspondía")
        return fallas

    @staticmethod
    def _mensaje_de_falla(que: str, error: Exception, fallas: list[str]) -> str:
        texto = f"{que}: {error}"
        if fallas:
            texto += " Además no se pudo devolver todo a como estaba; revisá el stock en Tiendanube: " + "; ".join(fallas) + "."
        else:
            texto += " No se cambió nada."
        return texto

    # -- Pedidos: confirmar y cancelar mueven el stock de Tiendanube ----------------

    def confirmar(self, codigo: str) -> list[dict]:
        if not self.activo():
            return self._base.confirmar(codigo)
        _, datos = self._base.reclamar(codigo, ("nuevo",), "confirmado")
        hechos: list[tuple[int, int, int]] = []
        try:
            items = almacen.agrupar_items(datos["items"])
            frescos = self._frescos({i["producto"] for i in items})
            faltantes: list[dict] = []
            plan: list[tuple[dict, int, int]] = []
            for it in items:
                v = self._variante(frescos.get(it["producto"]), it["talle"])
                hay = _stock_de(v) or 0
                if v is None or hay < it["cantidad"]:
                    faltantes.append({"producto": it["producto"], "talle": it["talle"], "pidio": it["cantidad"], "hay": hay})
                else:
                    plan.append((it, v["id"], hay))
            if faltantes:
                raise almacen.FaltaStock(faltantes)
            ids = self._ids_de({it["producto"] for it in items})
            movimientos: list[dict] = []
            for it, variante, hay in plan:
                despues = self._tn.ajustar_stock(it["producto"], variante, -it["cantidad"])
                hechos.append((it["producto"], variante, -it["cantidad"]))
                movimientos.append(
                    {"id": ids.get((it["producto"], almacen.normalizar(it["talle"])), 0), "producto_id": it["producto"], "talle": it["talle"], "antes": hay, "despues": despues}
                )
            self.invalidar()
            return movimientos
        except BaseException as e:
            fallas = self._devolver_ajustando(hechos)
            self._base.fijar_estado(codigo, "nuevo")
            self.invalidar()
            if isinstance(e, self._tn.ErrorDeTiendanube):
                raise almacen.ErrorDeTienda(self._mensaje_de_falla("No se pudo descontar el stock en Tiendanube", e, fallas)) from None
            if fallas and isinstance(e, almacen.ErrorDeAlmacen):
                raise almacen.ErrorDeTienda(self._mensaje_de_falla(str(e), e, fallas)) from None
            raise

    def cancelar(self, codigo: str) -> tuple[str, list[dict]]:
        if not self.activo():
            return self._base.cancelar(codigo)
        anterior, datos = self._base.reclamar(codigo, ("nuevo", "confirmado"), "cancelado")
        if anterior != "confirmado":
            return anterior, []  # nunca descontó nada: no hay stock que devolver
        hechos: list[tuple[int, int, int]] = []
        try:
            items = almacen.agrupar_items(datos["items"])
            frescos = self._frescos({i["producto"] for i in items})
            ids = self._ids_de({it["producto"] for it in items})
            movimientos: list[dict] = []
            for it in items:
                v = self._variante(frescos.get(it["producto"]), it["talle"])
                if v is None or _stock_de(v) is None:
                    raise almacen.ErrorDeTienda(f"El talle {it['talle']} ya no existe en Tiendanube: no se pudo devolver el stock de ese talle.")
                hay = _stock_de(v) or 0
                despues = self._tn.ajustar_stock(it["producto"], v["id"], it["cantidad"])
                hechos.append((it["producto"], v["id"], it["cantidad"]))
                movimientos.append(
                    {"id": ids.get((it["producto"], almacen.normalizar(it["talle"])), 0), "producto_id": it["producto"], "talle": it["talle"], "antes": hay, "despues": despues}
                )
            self.invalidar()
            return anterior, movimientos
        except BaseException as e:
            fallas = self._devolver_ajustando(hechos)
            self._base.fijar_estado(codigo, "confirmado")
            self.invalidar()
            if isinstance(e, self._tn.ErrorDeTiendanube):
                raise almacen.ErrorDeTienda(self._mensaje_de_falla("No se pudo devolver el stock a Tiendanube", e, fallas)) from None
            if fallas and isinstance(e, almacen.ErrorDeAlmacen):
                raise almacen.ErrorDeTienda(self._mensaje_de_falla(str(e), e, fallas)) from None
            raise

    # -- Para el panel (Cuenta → Stock compartido) ----------------------------------

    def comparar(self) -> dict:
        """Cuánto hay en el stock propio y cuánto en Tiendanube, para decidir antes de activar."""
        base = self._base.filas()
        todos = self._todos(forzar=True)
        distintos = 0
        propias = tienda = 0
        for f in base:
            en_tienda = self._cantidad(todos.get(f["producto_id"]), f["talle"])
            propias += f["cantidad"]
            tienda += en_tienda
            distintos += en_tienda != f["cantidad"]
        return {"modelos": len({f["producto_id"] for f in base}), "talles": len(base), "unidadesPropias": propias, "unidadesTienda": tienda, "talleDistintos": distintos}

    def probar_escritura(self) -> None:
        """Verifica que la app de Tiendanube puede escribir stock, sin cambiar nada (suma 0 a una variante).

        Levanta ErrorDeTienda si no puede (por ejemplo, si le falta el permiso de escribir productos).
        """
        todos = self._todos(forzar=True)
        for modelo in self._base.incluidos():
            for v in (todos.get(modelo) or {}).get("variants") or []:
                if _stock_de(v) is not None:
                    try:
                        self._tn.ajustar_stock(modelo, v["id"], 0)
                    except self._tn.ErrorDeTiendanube as e:
                        raise almacen.ErrorDeTienda(f"Tiendanube no dejó escribir el stock: {e}") from None
                    return
        raise almacen.ErrorDeTienda("No hay ninguna variante con stock contado para probar la escritura.")
