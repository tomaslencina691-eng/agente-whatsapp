"""La base propia del mayorista: el stock y los pedidos.

El stock del mayorista es SOLO del mayorista. No se lee de Tiendanube ni se le
escribe: Tiendanube aporta el nombre, las fotos y las categorías de cada modelo,
y nada más. Un modelo está "en el mayorista" cuando tiene filas acá.

Vive en Postgres (la misma instancia del agente, tablas con prefijo
`mayorista_`). Todo lo que toca varias filas va en una sola transacción: o se
aplica entero o no se aplica nada.

  · mayorista_stock    una fila por modelo y talle, con la cantidad.
  · mayorista_pedidos  lo que arma la gente en la web, con un código (A-7K3QX)
                       que viaja en el mensaje de WhatsApp. Al confirmar un
                       pedido se descuenta el stock; al cancelarlo, se devuelve.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone

DSN = os.getenv("POSTGRES_DSN", "").strip()

CREAR_TABLAS = """
CREATE TABLE IF NOT EXISTS mayorista_stock (
    id          BIGSERIAL PRIMARY KEY,
    producto_id BIGINT      NOT NULL,
    talle       TEXT        NOT NULL,
    cantidad    INTEGER     NOT NULL CHECK (cantidad >= 0),
    actualizado TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS mayorista_stock_talle ON mayorista_stock (producto_id, upper(talle));

CREATE TABLE IF NOT EXISTS mayorista_pedidos (
    codigo      TEXT        PRIMARY KEY,
    fecha       TIMESTAMPTZ NOT NULL DEFAULT now(),
    estado      TEXT        NOT NULL DEFAULT 'nuevo',
    datos       JSONB       NOT NULL,
    unidades    INTEGER     NOT NULL,
    total       BIGINT,
    actualizado TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS mayorista_pedidos_estado ON mayorista_pedidos (estado, fecha DESC);
"""

ESTADOS = ("nuevo", "confirmado", "despachado", "cancelado")


class ErrorDeAlmacen(Exception):
    """Base de los errores de acá. El mensaje es para mostrar."""


class Conflicto(ErrorDeAlmacen):
    """Alguien cambió el stock mientras se guardaba: no se aplicó nada."""


class PedidoNoExiste(ErrorDeAlmacen):
    pass


class PedidoNoSePuede(ErrorDeAlmacen):
    """El pedido está en un estado que no admite esa acción."""


class FaltaStock(ErrorDeAlmacen):
    """No alcanza la mercadería para confirmar el pedido. `faltantes` dice qué."""

    def __init__(self, faltantes: list[dict]) -> None:
        super().__init__("No alcanza el stock para confirmar este pedido.")
        self.faltantes = faltantes


def normalizar(talle: str) -> str:
    return " ".join(str(talle).upper().split())


def limpiar_talle(talle: str) -> str:
    return " ".join(str(talle).split())


def agrupar_items(items: list[dict]) -> list[dict]:
    """Junta las líneas repetidas del mismo modelo y talle (suma las cantidades)."""
    juntos: dict[tuple[int, str], dict] = {}
    for it in items:
        clave = (int(it["producto"]), normalizar(it["talle"]))
        if clave in juntos:
            juntos[clave]["cantidad"] += int(it["cantidad"])
        else:
            juntos[clave] = {"producto": int(it["producto"]), "talle": limpiar_talle(it["talle"]), "cantidad": int(it["cantidad"])}
    return list(juntos.values())


def _fila(id, producto_id, talle, cantidad) -> dict:
    return {"id": id, "producto_id": producto_id, "talle": talle, "cantidad": cantidad}


# -- Memoria (para las pruebas) -----------------------------------------------


class AlmacenMemoria:
    """Lo mismo que AlmacenPG, sin base."""

    def __init__(self) -> None:
        self._candado = threading.RLock()
        self._stock: dict[int, dict] = {}
        self._siguiente = 1
        self._pedidos: dict[str, dict] = {}

    def preparar(self) -> None:
        pass

    # stock
    def vacio(self) -> bool:
        return not self._stock

    def filas(self, productos=None) -> list[dict]:
        with self._candado:
            ids = None if productos is None else set(productos)
            return [dict(f) for f in sorted(self._stock.values(), key=lambda f: f["id"]) if ids is None or f["producto_id"] in ids]

    def incluidos(self) -> set[int]:
        with self._candado:
            return {f["producto_id"] for f in self._stock.values()}

    def _buscar(self, producto: int, talle: str) -> dict | None:
        return next((f for f in self._stock.values() if f["producto_id"] == producto and normalizar(f["talle"]) == normalizar(talle)), None)

    def aplicar(self, cambios: list[tuple[int, int, int]], altas: list[tuple[int, str, int]]) -> list[int]:
        """cambios = [(id de fila, cantidad que se esperaba, cantidad nueva)]; altas = [(modelo, talle, cantidad)]."""
        with self._candado:
            for fid, antes, _ in cambios:
                f = self._stock.get(fid)
                if f is None or f["cantidad"] != antes:
                    raise Conflicto("El stock cambió mientras se guardaba.")
            vistos = set()
            for producto, talle, _ in altas:
                clave = (producto, normalizar(talle))
                if clave in vistos or self._buscar(producto, talle):
                    raise Conflicto("Ese talle ya existe.")
                vistos.add(clave)
            for fid, _, despues in cambios:
                self._stock[fid]["cantidad"] = despues
            nuevos = []
            for producto, talle, cantidad in altas:
                fid = self._siguiente
                self._siguiente += 1
                self._stock[fid] = _fila(fid, producto, limpiar_talle(talle), cantidad)
                nuevos.append(fid)
            return nuevos

    def cargar(self, filas: list[tuple[int, str, int]]) -> int:
        """Agrega las que no existen. Devuelve cuántas agregó."""
        with self._candado:
            n = 0
            for producto, talle, cantidad in filas:
                if self._buscar(producto, talle):
                    continue
                fid = self._siguiente
                self._siguiente += 1
                self._stock[fid] = _fila(fid, producto, limpiar_talle(talle), cantidad)
                n += 1
            return n

    # pedidos
    def crear_pedido(self, codigo: str, datos: dict, unidades: int, total: int | None) -> bool:
        with self._candado:
            if codigo in self._pedidos:
                return False
            self._pedidos[codigo] = {"codigo": codigo, "fecha": datetime.now(timezone.utc).isoformat(), "estado": "nuevo", "unidades": unidades, "total": total, "datos": datos}
            return True

    def pedidos(self, estado: str | None = None, limite: int = 200) -> list[dict]:
        with self._candado:
            lista = [p for p in self._pedidos.values() if estado is None or p["estado"] == estado]
            return [dict(p) for p in sorted(lista, key=lambda p: p["fecha"], reverse=True)[:limite]]

    def pedido(self, codigo: str) -> dict | None:
        with self._candado:
            p = self._pedidos.get(codigo)
            return dict(p) if p else None

    def cuenta_por_estado(self) -> dict[str, int]:
        with self._candado:
            salida: dict[str, int] = {}
            for p in self._pedidos.values():
                salida[p["estado"]] = salida.get(p["estado"], 0) + 1
            return salida

    def _existente(self, codigo: str) -> dict:
        p = self._pedidos.get(codigo)
        if p is None:
            raise PedidoNoExiste("No existe ese pedido.")
        return p

    def confirmar(self, codigo: str) -> list[dict]:
        with self._candado:
            p = self._existente(codigo)
            if p["estado"] != "nuevo":
                raise PedidoNoSePuede(f"El pedido está {p['estado']}.")
            items = agrupar_items(p["datos"]["items"])
            faltantes = []
            for it in items:
                f = self._buscar(it["producto"], it["talle"])
                hay = f["cantidad"] if f else 0
                if hay < it["cantidad"]:
                    faltantes.append({"producto": it["producto"], "talle": it["talle"], "pidio": it["cantidad"], "hay": hay})
            if faltantes:
                raise FaltaStock(faltantes)
            movimientos = []
            for it in items:
                f = self._buscar(it["producto"], it["talle"])
                movimientos.append({"id": f["id"], "producto_id": f["producto_id"], "talle": f["talle"], "antes": f["cantidad"], "despues": f["cantidad"] - it["cantidad"]})
                f["cantidad"] -= it["cantidad"]
            p["estado"] = "confirmado"
            return movimientos

    def cancelar(self, codigo: str) -> tuple[str, list[dict]]:
        with self._candado:
            p = self._existente(codigo)
            anterior = p["estado"]
            if anterior not in ("nuevo", "confirmado"):
                raise PedidoNoSePuede(f"El pedido está {anterior}.")
            movimientos = []
            if anterior == "confirmado":
                for it in agrupar_items(p["datos"]["items"]):
                    f = self._buscar(it["producto"], it["talle"])
                    if f is None:
                        fid = self._siguiente
                        self._siguiente += 1
                        f = self._stock[fid] = _fila(fid, it["producto"], it["talle"], 0)
                    movimientos.append({"id": f["id"], "producto_id": f["producto_id"], "talle": f["talle"], "antes": f["cantidad"], "despues": f["cantidad"] + it["cantidad"]})
                    f["cantidad"] += it["cantidad"]
            p["estado"] = "cancelado"
            return anterior, movimientos

    def despachar(self, codigo: str) -> None:
        with self._candado:
            p = self._existente(codigo)
            if p["estado"] != "confirmado":
                raise PedidoNoSePuede(f"El pedido está {p['estado']}: solo se despacha uno confirmado.")
            p["estado"] = "despachado"


# -- Postgres -----------------------------------------------------------------


class AlmacenPG:
    """El almacén de verdad, en Postgres."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _conectar(self):
        import psycopg

        # Como connection de psycopg: al salir del "with" confirma, o deshace si hubo un error.
        return psycopg.connect(self.dsn)

    def preparar(self) -> None:
        with self._conectar() as c:
            c.execute(CREAR_TABLAS)

    # stock
    def vacio(self) -> bool:
        with self._conectar() as c, c.cursor() as cur:
            cur.execute("SELECT NOT EXISTS (SELECT 1 FROM mayorista_stock)")
            return bool(cur.fetchone()[0])

    def filas(self, productos=None) -> list[dict]:
        sql = "SELECT id, producto_id, talle, cantidad FROM mayorista_stock"
        parametros: tuple = ()
        if productos is not None:
            sql += " WHERE producto_id = ANY(%s)"
            parametros = (list(productos),)
        with self._conectar() as c, c.cursor() as cur:
            cur.execute(sql + " ORDER BY id", parametros)
            return [_fila(*f) for f in cur.fetchall()]

    def incluidos(self) -> set[int]:
        with self._conectar() as c, c.cursor() as cur:
            cur.execute("SELECT DISTINCT producto_id FROM mayorista_stock")
            return {f[0] for f in cur.fetchall()}

    def aplicar(self, cambios: list[tuple[int, int, int]], altas: list[tuple[int, str, int]]) -> list[int]:
        nuevos: list[int] = []
        with self._conectar() as c, c.cursor() as cur:
            for fid, antes, despues in cambios:
                cur.execute(
                    "UPDATE mayorista_stock SET cantidad = %s, actualizado = now() WHERE id = %s AND cantidad = %s",
                    (despues, fid, antes),
                )
                if cur.rowcount != 1:
                    raise Conflicto("El stock cambió mientras se guardaba.")
            for producto, talle, cantidad in altas:
                cur.execute(
                    "INSERT INTO mayorista_stock (producto_id, talle, cantidad) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING RETURNING id",
                    (producto, limpiar_talle(talle), cantidad),
                )
                fila = cur.fetchone()
                if fila is None:
                    raise Conflicto("Ese talle ya existe.")
                nuevos.append(fila[0])
        return nuevos

    def cargar(self, filas: list[tuple[int, str, int]]) -> int:
        if not filas:
            return 0
        with self._conectar() as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM mayorista_stock")
            antes = cur.fetchone()[0]
            cur.executemany(
                "INSERT INTO mayorista_stock (producto_id, talle, cantidad) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                [(p, limpiar_talle(t), n) for p, t, n in filas],
            )
            cur.execute("SELECT count(*) FROM mayorista_stock")
            return cur.fetchone()[0] - antes

    # pedidos
    def crear_pedido(self, codigo: str, datos: dict, unidades: int, total: int | None) -> bool:
        from psycopg.types.json import Jsonb

        with self._conectar() as c, c.cursor() as cur:
            cur.execute(
                "INSERT INTO mayorista_pedidos (codigo, datos, unidades, total) VALUES (%s, %s, %s, %s) ON CONFLICT (codigo) DO NOTHING",
                (codigo, Jsonb(datos), unidades, total),
            )
            return cur.rowcount == 1

    @staticmethod
    def _pedido(f) -> dict:
        return {"codigo": f[0], "fecha": f[1].isoformat(), "estado": f[2], "unidades": f[3], "total": f[4], "datos": f[5]}

    def pedidos(self, estado: str | None = None, limite: int = 200) -> list[dict]:
        sql = "SELECT codigo, fecha, estado, unidades, total, datos FROM mayorista_pedidos"
        parametros: list = []
        if estado:
            sql += " WHERE estado = %s"
            parametros.append(estado)
        sql += " ORDER BY fecha DESC LIMIT %s"
        parametros.append(int(limite))
        with self._conectar() as c, c.cursor() as cur:
            cur.execute(sql, parametros)
            return [self._pedido(f) for f in cur.fetchall()]

    def pedido(self, codigo: str) -> dict | None:
        with self._conectar() as c, c.cursor() as cur:
            cur.execute("SELECT codigo, fecha, estado, unidades, total, datos FROM mayorista_pedidos WHERE codigo = %s", (codigo,))
            f = cur.fetchone()
            return self._pedido(f) if f else None

    def cuenta_por_estado(self) -> dict[str, int]:
        with self._conectar() as c, c.cursor() as cur:
            cur.execute("SELECT estado, count(*) FROM mayorista_pedidos GROUP BY estado")
            return {e: n for e, n in cur.fetchall()}

    @staticmethod
    def _bloquear(cur, codigo: str) -> tuple[str, dict]:
        cur.execute("SELECT estado, datos FROM mayorista_pedidos WHERE codigo = %s FOR UPDATE", (codigo,))
        f = cur.fetchone()
        if f is None:
            raise PedidoNoExiste("No existe ese pedido.")
        return f[0], f[1]

    def confirmar(self, codigo: str) -> list[dict]:
        with self._conectar() as c, c.cursor() as cur:
            estado, datos = self._bloquear(cur, codigo)
            if estado != "nuevo":
                raise PedidoNoSePuede(f"El pedido está {estado}.")
            movimientos: list[dict] = []
            faltantes: list[dict] = []
            for it in agrupar_items(datos["items"]):
                # El "cantidad >= n" es lo que impide vender de más, aunque dos confirmen a la vez.
                cur.execute(
                    "UPDATE mayorista_stock SET cantidad = cantidad - %s, actualizado = now() "
                    "WHERE producto_id = %s AND upper(talle) = upper(%s) AND cantidad >= %s RETURNING id, producto_id, talle, cantidad",
                    (it["cantidad"], it["producto"], it["talle"], it["cantidad"]),
                )
                f = cur.fetchone()
                if f:
                    movimientos.append({"id": f[0], "producto_id": f[1], "talle": f[2], "antes": f[3] + it["cantidad"], "despues": f[3]})
                    continue
                cur.execute("SELECT cantidad FROM mayorista_stock WHERE producto_id = %s AND upper(talle) = upper(%s)", (it["producto"], it["talle"]))
                hay = cur.fetchone()
                faltantes.append({"producto": it["producto"], "talle": it["talle"], "pidio": it["cantidad"], "hay": hay[0] if hay else 0})
            if faltantes:
                raise FaltaStock(faltantes)  # al salir con error, se deshace todo
            cur.execute("UPDATE mayorista_pedidos SET estado = 'confirmado', actualizado = now() WHERE codigo = %s", (codigo,))
            return movimientos

    def cancelar(self, codigo: str) -> tuple[str, list[dict]]:
        with self._conectar() as c, c.cursor() as cur:
            estado, datos = self._bloquear(cur, codigo)
            if estado not in ("nuevo", "confirmado"):
                raise PedidoNoSePuede(f"El pedido está {estado}.")
            movimientos: list[dict] = []
            if estado == "confirmado":
                for it in agrupar_items(datos["items"]):
                    cur.execute(
                        "UPDATE mayorista_stock SET cantidad = cantidad + %s, actualizado = now() "
                        "WHERE producto_id = %s AND upper(talle) = upper(%s) RETURNING id, producto_id, talle, cantidad",
                        (it["cantidad"], it["producto"], it["talle"]),
                    )
                    f = cur.fetchone()
                    if f is None:  # el talle se había borrado: se vuelve a crear
                        cur.execute(
                            "INSERT INTO mayorista_stock (producto_id, talle, cantidad) VALUES (%s, %s, %s) RETURNING id, producto_id, talle, cantidad",
                            (it["producto"], it["talle"], it["cantidad"]),
                        )
                        f = cur.fetchone()
                    movimientos.append({"id": f[0], "producto_id": f[1], "talle": f[2], "antes": f[3] - it["cantidad"], "despues": f[3]})
            cur.execute("UPDATE mayorista_pedidos SET estado = 'cancelado', actualizado = now() WHERE codigo = %s", (codigo,))
            return estado, movimientos

    def despachar(self, codigo: str) -> None:
        with self._conectar() as c, c.cursor() as cur:
            estado, _ = self._bloquear(cur, codigo)
            if estado != "confirmado":
                raise PedidoNoSePuede(f"El pedido está {estado}: solo se despacha uno confirmado.")
            cur.execute("UPDATE mayorista_pedidos SET estado = 'despachado', actualizado = now() WHERE codigo = %s", (codigo,))


def crear() -> AlmacenPG | None:
    return AlmacenPG(DSN) if DSN else None
