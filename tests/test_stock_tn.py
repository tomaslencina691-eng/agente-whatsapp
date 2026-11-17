"""Pruebas del stock compartido con Tiendanube (mayorista/stock_tn.py). No salen a internet ni usan base.

Lo que se cuida acá:

  · Apagado, el mayorista sigue con su stock propio.
  · Prendido, el stock que se ve es el de Tiendanube, y confirmar un pedido lo descuenta ALLÁ.
  · Dos confirmaciones del mismo pedido no descuentan dos veces.
  · Si algo falla a mitad, lo ya descontado se devuelve y el pedido vuelve a su estado.
  · Cancelar un pedido confirmado devuelve el stock a Tiendanube; cancelar uno nuevo no toca nada.
  · Guardar la planilla escribe en Tiendanube, y avisa si el stock cambió en el medio.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mayorista"))

import acceso  # noqa: E402
import admin  # noqa: E402
import ajustes  # noqa: E402
import almacen  # noqa: E402
import historial as hist  # noqa: E402
import stock_tn  # noqa: E402
import tn  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

H = {"x-mayorista": "1"}
CLAVE = "clave-de-prueba-123"


class ErrorDeTiendanube(Exception):
    pass


class TiendaConEscritura:
    """Una tienda en memoria que además deja cambiar el stock, como la API de Tiendanube."""

    ErrorDeTiendanube = ErrorDeTiendanube

    def __init__(self):
        self.productos = {
            1: self._producto(1, {"S": 0, "M": 5, "L": 3}),
            2: self._producto(2, {"M": 4}),
        }
        self.llamadas: list[tuple] = []
        self.fallar_en: int | None = None  # número de llamada de escritura que falla
        self.sin_permiso = False

    @staticmethod
    def _producto(id, talles):
        return {"id": id, "variants": [{"id": id * 10 + i, "stock": s, "values": [{"es": t}]} for i, (t, s) in enumerate(talles.items())]}

    def stock(self, producto, talle):
        return next(v["stock"] for v in self.productos[producto]["variants"] if v["values"][0]["es"] == talle)

    def agregar_talle(self, producto, talle, stock):
        self.productos[producto]["variants"].append({"id": producto * 100 + len(self.productos[producto]["variants"]), "stock": stock, "values": [{"es": talle}]})

    def todos_los_productos(self):
        return list(self.productos.values())

    def productos_por_id(self, ids):
        return {i: self.productos[i] for i in ids if i in self.productos}

    def _escribir(self, producto, variante, calcular):
        n = len([c for c in self.llamadas if c[0] in ("ajustar", "fijar")]) + 1
        if self.sin_permiso:
            raise ErrorDeTiendanube("403 sin permiso")
        if self.fallar_en == n:
            self.fallar_en = None  # falla una sola vez: lo que se intente después (devolver lo hecho) sale bien
            raise ErrorDeTiendanube("Tiendanube se cayó")
        v = next(v for v in self.productos[producto]["variants"] if v["id"] == variante)
        v["stock"] = calcular(v["stock"])
        return v["stock"]

    def ajustar_stock(self, producto, variante, cambio):
        r = self._escribir(producto, variante, lambda s: max(0, s + cambio))
        self.llamadas.append(("ajustar", producto, variante, cambio))
        return r

    def fijar_stock(self, producto, variante, cantidad):
        r = self._escribir(producto, variante, lambda s: cantidad)
        self.llamadas.append(("fijar", producto, variante, cantidad))
        return r


@pytest.fixture
def tienda():
    return TiendaConEscritura()


@pytest.fixture
def base():
    """El mayorista ofrece los modelos 1 y 2. Su stock PROPIO difiere del de la tienda a propósito."""
    a = almacen.AlmacenMemoria()
    a.cargar([(1, "S", 9), (1, "M", 9), (1, "L", 9), (2, "M", 9)])
    return a


@pytest.fixture
def alm(base, tienda):
    a = stock_tn.AlmacenConTiendanube(base, tienda)
    a.fijar_activo(True)
    return a


def hay(a, producto, talle):
    return next(f["cantidad"] for f in a.filas([producto]) if f["talle"] == talle)


def pedido(base, codigo, *items):
    datos = {"items": [{"producto": p, "talle": t, "cantidad": n, "nombre": "Boca"} for p, t, n in items], "nombre": "Pablo", "negocio": "", "notas": ""}
    base.crear_pedido(codigo, datos, sum(n for _, _, n in items), 100000)


# -- Leer ---------------------------------------------------------------------------


def test_apagado_el_mayorista_sigue_con_su_stock_propio(base, tienda):
    a = stock_tn.AlmacenConTiendanube(base, tienda)

    assert hay(a, 1, "M") == 9, "no se lee Tiendanube"
    pedido(base, "A-AAAAA", (1, "M", 2))
    a.confirmar("A-AAAAA")
    assert hay(a, 1, "M") == 7 and tienda.stock(1, "M") == 5 and tienda.llamadas == []


def test_prendido_el_stock_que_se_ve_es_el_de_tiendanube(alm, tienda):
    assert (hay(alm, 1, "S"), hay(alm, 1, "M"), hay(alm, 1, "L")) == (0, 5, 3)


def test_una_venta_de_la_tienda_se_ve_en_el_mayorista(alm, tienda):
    tienda.productos[1]["variants"][1]["stock"] = 1  # alguien compró 4 por la tienda online
    alm.invalidar()

    assert hay(alm, 1, "M") == 1


def test_un_talle_nuevo_en_la_tienda_aparece_solo(alm, tienda):
    tienda.agregar_talle(1, "XL", 6)
    alm.invalidar()

    assert hay(alm, 1, "XL") == 6


def test_una_variante_sin_stock_contado_no_se_ofrece(alm, tienda):
    tienda.agregar_talle(1, "XXL", None)
    alm.invalidar()

    assert "XXL" not in [f["talle"] for f in alm.filas([1])]


# -- Confirmar ----------------------------------------------------------------------


def test_confirmar_descuenta_el_stock_en_tiendanube(alm, base, tienda):
    pedido(base, "A-AAAAA", (1, "M", 2), (2, "M", 1))

    movs = alm.confirmar("A-AAAAA")

    assert (tienda.stock(1, "M"), tienda.stock(2, "M")) == (3, 3)
    assert base.pedido("A-AAAAA")["estado"] == "confirmado"
    assert sorted((m["producto_id"], m["antes"], m["despues"]) for m in movs) == [(1, 5, 3), (2, 4, 3)]
    assert all(m["id"] for m in movs), "cada movimiento lleva el id de su fila, para el historial"


def test_confirmar_dos_veces_no_descuenta_dos_veces(alm, base, tienda):
    pedido(base, "A-AAAAA", (1, "M", 2))
    alm.confirmar("A-AAAAA")

    with pytest.raises(almacen.PedidoNoSePuede):
        alm.confirmar("A-AAAAA")

    assert tienda.stock(1, "M") == 3


def test_si_no_alcanza_en_tiendanube_no_se_descuenta_nada_y_el_pedido_sigue_nuevo(alm, base, tienda):
    pedido(base, "A-AAAAA", (1, "M", 2), (1, "L", 4))  # de L hay 3

    with pytest.raises(almacen.FaltaStock) as e:
        alm.confirmar("A-AAAAA")

    assert e.value.faltantes == [{"producto": 1, "talle": "L", "pidio": 4, "hay": 3}]
    assert (tienda.stock(1, "M"), tienda.stock(1, "L")) == (5, 3)
    assert base.pedido("A-AAAAA")["estado"] == "nuevo"


def test_el_stock_que_cuenta_es_el_fresco_aunque_lo_guardado_este_viejo(alm, base, tienda):
    hay(alm, 1, "M")  # queda guardado: 5
    tienda.productos[1]["variants"][1]["stock"] = 1  # y justo después se vende por la tienda online
    pedido(base, "A-AAAAA", (1, "M", 2))

    with pytest.raises(almacen.FaltaStock):
        alm.confirmar("A-AAAAA")

    assert tienda.stock(1, "M") == 1


def test_si_tiendanube_falla_a_mitad_se_devuelve_lo_descontado(alm, base, tienda):
    pedido(base, "A-AAAAA", (1, "M", 2), (2, "M", 1))
    tienda.fallar_en = 2  # el primer descuento sale, el segundo no

    with pytest.raises(almacen.ErrorDeTienda) as e:
        alm.confirmar("A-AAAAA")

    assert (tienda.stock(1, "M"), tienda.stock(2, "M")) == (5, 4), "lo que se había descontado volvió"
    assert base.pedido("A-AAAAA")["estado"] == "nuevo"
    assert "No se cambió nada" in str(e.value)


def test_si_ademas_no_se_puede_devolver_lo_descontado_se_avisa_que_hay_que_revisar_tiendanube(alm, base, tienda):
    pedido(base, "A-AAAAA", (1, "M", 2), (2, "M", 1))
    original = tienda.ajustar_stock

    def se_cae_despues_del_primero(producto, variante, cambio):
        if tienda.llamadas:
            raise ErrorDeTiendanube("Tiendanube sigue caída")
        return original(producto, variante, cambio)

    tienda.ajustar_stock = se_cae_despues_del_primero

    with pytest.raises(almacen.ErrorDeTienda) as e:
        alm.confirmar("A-AAAAA")

    assert "revisá el stock en Tiendanube" in str(e.value) and "modelo 1" in str(e.value)
    assert base.pedido("A-AAAAA")["estado"] == "nuevo"


def test_se_puede_reintentar_despues_de_una_falla(alm, base, tienda):
    pedido(base, "A-AAAAA", (1, "M", 2))
    tienda.fallar_en = 1
    with pytest.raises(almacen.ErrorDeTienda):
        alm.confirmar("A-AAAAA")
    tienda.fallar_en = None

    alm.confirmar("A-AAAAA")

    assert tienda.stock(1, "M") == 3 and base.pedido("A-AAAAA")["estado"] == "confirmado"


# -- Cancelar -----------------------------------------------------------------------


def test_cancelar_un_pedido_confirmado_devuelve_el_stock_a_tiendanube(alm, base, tienda):
    pedido(base, "A-AAAAA", (1, "M", 2))
    alm.confirmar("A-AAAAA")

    anterior, movs = alm.cancelar("A-AAAAA")

    assert anterior == "confirmado" and tienda.stock(1, "M") == 5
    assert [(m["antes"], m["despues"]) for m in movs] == [(3, 5)]
    assert base.pedido("A-AAAAA")["estado"] == "cancelado"


def test_cancelar_un_pedido_nuevo_no_toca_tiendanube(alm, base, tienda):
    pedido(base, "A-AAAAA", (1, "M", 2))

    anterior, movs = alm.cancelar("A-AAAAA")

    assert (anterior, movs) == ("nuevo", []) and tienda.llamadas == [] and tienda.stock(1, "M") == 5


def test_si_falla_devolver_el_stock_el_pedido_sigue_confirmado(alm, base, tienda):
    pedido(base, "A-AAAAA", (1, "M", 2))
    alm.confirmar("A-AAAAA")
    tienda.fallar_en = 2  # la llamada 1 fue el descuento; la 2, la devolución

    with pytest.raises(almacen.ErrorDeTienda):
        alm.cancelar("A-AAAAA")

    assert base.pedido("A-AAAAA")["estado"] == "confirmado" and tienda.stock(1, "M") == 3


# -- Guardar la planilla --------------------------------------------------------------


def cambios_de(alm, producto, talle, despues, antes=None):
    f = next(f for f in alm.filas([producto]) if f["talle"] == talle)
    return (f["id"], f["cantidad"] if antes is None else antes, despues)


def test_editar_la_planilla_escribe_en_tiendanube(alm, tienda):
    alm.aplicar([cambios_de(alm, 1, "M", 8), cambios_de(alm, 1, "L", 0)], [])

    assert (tienda.stock(1, "M"), tienda.stock(1, "L")) == (8, 0)
    assert hay(alm, 1, "M") == 8


def test_si_el_stock_cambio_mientras_se_editaba_no_se_pisa(alm, tienda):
    cambio = cambios_de(alm, 1, "M", 8)
    tienda.productos[1]["variants"][1]["stock"] = 2  # una venta de la tienda online

    with pytest.raises(almacen.Conflicto):
        alm.aplicar([cambio], [])

    assert tienda.stock(1, "M") == 2


def test_si_falla_a_mitad_de_guardar_se_deshace_todo(alm, tienda):
    tienda.fallar_en = 2

    with pytest.raises(almacen.ErrorDeTienda):
        alm.aplicar([cambios_de(alm, 1, "M", 8), cambios_de(alm, 1, "L", 1)], [])

    assert (tienda.stock(1, "M"), tienda.stock(1, "L")) == (5, 3)


def test_cargar_un_talle_que_no_existe_en_tiendanube_avisa_que_hay_que_crearlo_alla(alm, tienda):
    with pytest.raises(almacen.Conflicto, match="creálo ahí primero"):
        alm.aplicar([], [(1, "XXL", 4)])


def test_un_talle_que_existe_en_tiendanube_pero_no_en_el_mayorista_se_carga(alm, base, tienda):
    tienda.agregar_talle(1, "XL", 0)  # todavía no se sincronizó: la alta lo encuentra en Tiendanube

    nuevos = alm.aplicar([], [(1, "XL", 6)])

    assert tienda.stock(1, "XL") == 6 and len(nuevos) == 1


# -- Para el panel ----------------------------------------------------------------------


def test_se_puede_comparar_el_stock_propio_con_el_de_tiendanube(base, tienda):
    a = stock_tn.AlmacenConTiendanube(base, tienda)

    c = a.comparar()

    assert c == {"modelos": 2, "talles": 4, "unidadesPropias": 36, "unidadesTienda": 12, "talleDistintos": 4}


def test_probar_la_escritura_no_cambia_el_stock(base, tienda):
    a = stock_tn.AlmacenConTiendanube(base, tienda)

    a.probar_escritura()

    assert tienda.llamadas and all(c[0] == "ajustar" and c[3] == 0 for c in tienda.llamadas)
    assert [tienda.stock(1, "M"), tienda.stock(1, "L")] == [5, 3]


def test_sin_permiso_de_escritura_la_prueba_falla_con_un_mensaje(base, tienda):
    a = stock_tn.AlmacenConTiendanube(base, tienda)
    tienda.sin_permiso = True

    with pytest.raises(almacen.ErrorDeTienda, match="no dejó escribir"):
        a.probar_escritura()


# -- Por el panel (HTTP) -----------------------------------------------------------------


@pytest.fixture
def web(monkeypatch, base, tienda):
    monkeypatch.setattr(acceso, "USUARIO", "leo")
    monkeypatch.setattr(acceso, "CLAVE", CLAVE)
    monkeypatch.setattr(acceso, "SECRETO", "s" * 40)
    monkeypatch.setattr(tn, "TIENDA", "1")
    monkeypatch.setattr(tn, "TOKEN", "token")
    acceso._fallos.clear()
    a = stock_tn.AlmacenConTiendanube(base, tienda)
    defecto = ajustes.por_defecto({}, "Mi Tienda", "5491100000000", "", True, 6, [])
    app = FastAPI()
    app.include_router(admin.crear_router(hist.HistorialMemoria(), lambda: None, a, [], lambda: dict(defecto)))
    c = TestClient(app)
    assert c.post("/admin/api/login", json={"usuario": "leo", "clave": CLAVE}, headers=H).status_code == 200
    c.almacen = a
    return c


def test_el_panel_muestra_el_estado_y_la_comparacion(web):
    d = web.get("/admin/api/stock-compartido").json()

    assert d["activo"] is False and d["comparacion"]["unidadesTienda"] == 12


def test_activar_prueba_la_escritura_y_prende_el_stock_compartido(web, tienda):
    r = web.post("/admin/api/stock-compartido/activar", headers=H)

    assert r.status_code == 200 and r.json()["activo"] is True
    assert web.almacen.activo() is True
    assert hay(web.almacen, 1, "M") == 5, "ahora rige el de Tiendanube"


def test_sin_permiso_de_escritura_no_se_activa(web, tienda):
    tienda.sin_permiso = True

    r = web.post("/admin/api/stock-compartido/activar", headers=H)

    assert r.status_code == 422 and "Escribir productos" in r.json()["detail"]
    assert web.almacen.activo() is False


def test_desactivar_vuelve_al_stock_propio(web):
    web.post("/admin/api/stock-compartido/activar", headers=H)

    r = web.post("/admin/api/stock-compartido/desactivar", headers=H)

    assert r.json() == {"activo": False}
    assert hay(web.almacen, 1, "M") == 9


def test_confirmar_por_el_panel_descuenta_en_tiendanube_y_si_falla_dice_por_que(web, base, tienda):
    web.post("/admin/api/stock-compartido/activar", headers=H)
    pedido(base, "A-AAAAA", (1, "M", 2))
    tienda.fallar_en = len(tienda.llamadas) + 1

    r = web.post("/admin/api/pedidos/A-AAAAA/confirmar", headers=H)

    assert r.status_code == 502 and "Tiendanube" in r.json()["detail"]["mensaje"]
    assert tienda.stock(1, "M") == 5
    tienda.fallar_en = None
    assert web.post("/admin/api/pedidos/A-AAAAA/confirmar", headers=H).status_code == 200
    assert tienda.stock(1, "M") == 3


def test_solo_el_duenio_maneja_el_stock_compartido():
    assert acceso.grupo_de("/admin/api/stock-compartido/activar") == "administrar"
    assert acceso.grupo_de("/admin/api/stock") == "stock"
