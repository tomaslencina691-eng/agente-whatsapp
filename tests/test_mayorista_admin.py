"""Pruebas del panel de administración del mayorista. No salen a internet ni usan base.

Tiendanube se reemplaza por una tienda de mentira en memoria y la base por
`AlmacenMemoria`: lo que se prueba es lo nuestro. Lo más importante de este archivo:

  · El stock del mayorista es PROPIO: tocarlo no cambia la tienda, y una venta en
    la tienda no lo cambia a él.
  · `test_un_conflicto_no_aplica_ni_los_cambios_buenos`: si entra un pedido
    confirmado mientras alguien edita la planilla, no se pisa el stock.
  · Confirmar un pedido que no alcanza no descuenta nada, ni de lo que sí había.
  · Todo lo que no es el login exige sesión.

    pytest
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mayorista"))

import acceso  # noqa: E402
import admin  # noqa: E402
import almacen  # noqa: E402
import historial as hist  # noqa: E402
import tn  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

CLAVE = "clave-de-prueba-123"
H = {"x-mayorista": "1"}


class TiendaFalsa:
    """Una tienda en memoria con la forma que devuelve la API de Tiendanube (solo lectura)."""

    def __init__(self):
        self.productos = {
            1: self._producto(1, "Boca Juniors - Titular 26/27", True, {"S": 0, "M": 2, "L": 3}),
            2: self._producto(2, "Argentina Retro", False, {"XL": 0}),  # oculto y sin stock
            3: self._producto(3, "River Plate - Titular 26/27", True, {"S": 1, "M": 4}),  # nuevo: la tienda lo tiene, el mayorista no
            4: self._producto(4, "Sin foto", True, {"M": 5}, fotos=False),
        }

    @staticmethod
    def _producto(id, nombre, publicado, talles, fotos=True):
        return {
            "id": id,
            "name": {"es": nombre},
            "published": publicado,
            "categories": [{"name": {"es": "FÚTBOL ARGENTINO"}}],
            "images": [{"position": 1, "src": "https://acdn-us.mitiendanube.com/x/a-1024-1024.png"}] if fotos else [],
            "variants": [
                {"id": id * 10 + i, "price": "75000.00", "stock": s, "stock_management": True, "values": [{"es": t}]}
                for i, (t, s) in enumerate(talles.items())
            ],
        }

    def vender(self, producto, talle, n=1):
        for v in self.productos[producto]["variants"]:
            if v["values"][0]["es"] == talle:
                v["stock"] -= n

    def stock(self, producto, talle):
        return next(v["stock"] for v in self.productos[producto]["variants"] if v["values"][0]["es"] == talle)

    def instalar(self, monkeypatch):
        monkeypatch.setattr(tn, "TIENDA", "1")
        monkeypatch.setattr(tn, "TOKEN", "token")
        monkeypatch.setattr(tn, "todos_los_productos", lambda: list(self.productos.values()))
        monkeypatch.setattr(tn, "productos_por_id", lambda ids: {i: self.productos[i] for i in ids if i in self.productos})


@pytest.fixture
def tienda(monkeypatch):
    t = TiendaFalsa()
    t.instalar(monkeypatch)
    return t


@pytest.fixture
def memoria():
    return hist.HistorialMemoria()


@pytest.fixture
def alm():
    """El mayorista arranca con los modelos 1 y 2 (copiados de la tienda): el 3 todavía no está."""
    a = almacen.AlmacenMemoria()
    a.cargar([(1, "S", 0), (1, "M", 2), (1, "L", 3), (2, "XL", 0)])
    return a


@pytest.fixture
def invalidaciones():
    return []


@pytest.fixture
def cliente(monkeypatch, tienda, memoria, alm, invalidaciones):
    monkeypatch.setattr(acceso, "USUARIO", "leo")
    monkeypatch.setattr(acceso, "CLAVE", CLAVE)
    monkeypatch.setattr(acceso, "SECRETO", "s" * 40)
    acceso._fallos.clear()
    app = FastAPI()
    app.include_router(admin.crear_router(memoria, lambda: invalidaciones.append(1), alm))
    return TestClient(app)


@pytest.fixture
def sesion(cliente):
    r = cliente.post("/admin/api/login", json={"usuario": "leo", "clave": CLAVE}, headers=H)
    assert r.status_code == 200
    return cliente


def fila(alm, producto, talle):
    return next(f for f in alm.filas([producto]) if f["talle"] == talle)


def hay(alm, producto, talle):
    return fila(alm, producto, talle)["cantidad"]


def cambio(alm, producto, talle, despues, antes=None):
    f = fila(alm, producto, talle)
    return {"tipo": "stock", "producto": producto, "variante": f["id"], "antes": f["cantidad"] if antes is None else antes, "despues": despues}


def pedido(alm, codigo, *items, estado="nuevo"):
    """Un pedido de la web: items = (modelo, talle, cantidad)."""
    datos = {"items": [{"producto": p, "talle": t, "cantidad": n, "nombre": "Boca Juniors - Titular 26/27"} for p, t, n in items], "nombre": "Pablo", "negocio": "Kiosco", "notas": ""}
    alm.crear_pedido(codigo, datos, sum(n for _, _, n in items), 100000)
    if estado != "nuevo":
        alm._pedidos[codigo]["estado"] = estado


# -- Entrar -------------------------------------------------------------------


@pytest.mark.parametrize(
    "metodo, ruta",
    [
        ("get", "/admin/api/stock"), ("get", "/admin/api/historial"), ("get", "/admin/api/pedidos"), ("get", "/admin/api/tienda/nuevos"),
        ("post", "/admin/api/stock/aplicar"), ("post", "/admin/api/pedidos/A-AAAAA/confirmar"), ("post", "/admin/api/tienda/incluir"),
        ("post", "/admin/api/tienda/cargar-stock"),
    ],
)
def test_sin_sesion_no_se_entra_a_nada(cliente, metodo, ruta):
    r = getattr(cliente, metodo)(ruta, headers=H, **({"json": {}} if metodo == "post" else {}))

    assert r.status_code == 401


def test_con_la_clave_correcta_se_entra(cliente):
    r = cliente.post("/admin/api/login", json={"usuario": "leo", "clave": CLAVE}, headers=H)

    assert r.status_code == 200
    assert "httponly" in r.headers["set-cookie"].lower()
    assert cliente.get("/admin/api/estado").json() == {"configurado": True, "usuario": "leo"}


@pytest.mark.parametrize("usuario, clave", [("leo", "mala"), ("otro", CLAVE), ("", ""), ("LEO", CLAVE)])
def test_con_datos_equivocados_no_se_entra(cliente, usuario, clave):
    r = cliente.post("/admin/api/login", json={"usuario": usuario, "clave": clave}, headers=H)

    assert r.status_code == 401
    assert cliente.get("/admin/api/estado").json()["usuario"] is None


def test_tras_cinco_fallos_se_bloquea_aunque_la_clave_sea_buena(cliente):
    for _ in range(5):
        cliente.post("/admin/api/login", json={"usuario": "leo", "clave": "mala"}, headers=H)

    r = cliente.post("/admin/api/login", json={"usuario": "leo", "clave": CLAVE}, headers=H)

    assert r.status_code == 429


def test_el_bloqueo_se_levanta_con_el_tiempo():
    acceso._fallos.clear()
    for _ in range(5):
        acceso.anotar_fallo("1.2.3.4", ahora=1000.0)

    assert acceso.bloqueado("1.2.3.4", ahora=1100.0)
    assert not acceso.bloqueado("1.2.3.4", ahora=1000.0 + acceso.VENTANA_SEGUNDOS + 1)


def test_salir_borra_la_sesion(sesion):
    sesion.post("/admin/api/logout", headers=H)

    assert sesion.get("/admin/api/stock").status_code == 401


def test_una_cookie_adulterada_no_sirve(cliente):
    valida = acceso.crear_sesion("leo")
    cuerpo, firma = valida.rsplit(".", 1)

    assert acceso.leer_sesion(valida) == "leo"
    assert acceso.leer_sesion(cuerpo + "." + "0" * len(firma)) is None
    assert acceso.leer_sesion("basura") is None
    assert acceso.leer_sesion(None) is None


def test_una_sesion_vencida_no_sirve():
    vieja = acceso.crear_sesion("leo", ahora=time.time() - acceso.DURACION_SEGUNDOS - 10)

    assert acceso.leer_sesion(vieja) is None


def test_un_secreto_corto_apaga_el_panel(cliente, monkeypatch):
    """Sin un secreto largo las cookies se podrían falsificar: mejor no tener panel."""
    monkeypatch.setattr(acceso, "SECRETO", "corto")

    assert cliente.get("/admin/api/estado").json()["configurado"] is False
    assert cliente.post("/admin/api/login", json={"usuario": "leo", "clave": CLAVE}, headers=H).status_code == 503


def test_los_cambios_exigen_el_header_propio(sesion, alm):
    """Es lo que impide que otra web mande cambios desde tu navegador."""
    r = sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(alm, 1, "M", 5)]})

    assert r.status_code == 403
    assert hay(alm, 1, "M") == 2


# -- Ver ----------------------------------------------------------------------


def test_la_planilla_trae_solo_los_modelos_del_mayorista_con_su_stock_propio(sesion):
    r = sesion.get("/admin/api/stock")

    productos = {p["id"]: p for p in r.json()["productos"]}
    assert set(productos) == {1, 2}, "el modelo 3 está en la tienda pero todavía no en el mayorista"
    assert productos[2]["publicado"] is False
    assert [(x["talle"], x["stock"]) for x in productos[1]["variantes"]] == [("S", 0), ("M", 2), ("L", 3)]
    assert r.json()["vacio"] is False


def test_el_stock_es_propio_una_venta_en_la_tienda_no_lo_cambia(sesion, tienda, alm):
    tienda.vender(1, "M", 2)

    productos = {p["id"]: p for p in sesion.get("/admin/api/stock").json()["productos"]}

    assert tienda.stock(1, "M") == 0
    assert hay(alm, 1, "M") == 2
    assert [x["stock"] for x in productos[1]["variantes"] if x["talle"] == "M"] == [2]


def test_un_modelo_borrado_de_la_tienda_sigue_en_la_planilla(sesion, tienda):
    del tienda.productos[2]

    productos = {p["id"]: p for p in sesion.get("/admin/api/stock").json()["productos"]}

    assert "ya no está en la tienda" in productos[2]["nombre"]


# -- Guardar ------------------------------------------------------------------


def test_guardar_cambia_el_stock_propio_y_lo_anota(sesion, tienda, alm, memoria):
    r = sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(alm, 1, "M", 7), cambio(alm, 1, "S", 4)]}, headers=H)

    assert r.status_code == 200
    assert r.json()["aplicados"] == 2
    assert (hay(alm, 1, "M"), hay(alm, 1, "S")) == (7, 4)
    assert tienda.stock(1, "M") == 2, "la tienda no se toca"
    assert {(e["talle"], e["antes"], e["despues"]) for e in memoria.filas} == {("M", 2, 7), ("S", 0, 4)}
    assert memoria.filas[0]["usuario"] == "leo"


def test_un_conflicto_no_aplica_ni_los_cambios_buenos(sesion, alm, memoria):
    """Se confirmó un pedido mientras editabas: no se pisa, y no se guarda nada."""
    mios = [cambio(alm, 1, "M", 9), cambio(alm, 1, "L", 8)]
    pedido(alm, "A-AAAAA", (1, "M", 1))
    alm.confirmar("A-AAAAA")  # entra un pedido en el medio

    r = sesion.post("/admin/api/stock/aplicar", json={"cambios": mios}, headers=H)

    assert r.status_code == 409
    conflicto = r.json()["detail"]["conflictos"][0]
    assert (conflicto["talle"], conflicto["esperado"], conflicto["actual"]) == ("M", 2, 1)
    assert hay(alm, 1, "L") == 3, "el cambio bueno tampoco se aplicó"
    assert memoria.filas == []


def test_si_la_base_detecta_el_choque_justo_al_guardar_tampoco_se_aplica(sesion, alm, monkeypatch):
    """Dos personas guardando a la vez: la segunda se entera en vez de pisar a la primera."""
    original = alm.aplicar

    def con_choque(cambios, altas):
        f = fila(alm, 1, "M")
        f["cantidad"] = 99  # alguien se adelantó entre la lectura y la escritura
        alm._stock[f["id"]]["cantidad"] = 99
        return original(cambios, altas)

    monkeypatch.setattr(alm, "aplicar", con_choque)

    r = sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(alm, 1, "M", 5)]}, headers=H)

    assert r.status_code == 409
    assert hay(alm, 1, "M") == 99


def test_no_se_guarda_stock_negativo(sesion, alm):
    r = sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(alm, 1, "M", -1)]}, headers=H)

    assert r.status_code == 422
    assert hay(alm, 1, "M") == 2


def test_guardar_sin_cambios_reales_no_hace_nada(sesion, alm, memoria):
    r = sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(alm, 1, "M", 2)]}, headers=H)

    assert r.status_code == 200
    assert r.json()["lote"] is None
    assert memoria.filas == []


def test_un_talle_de_otro_modelo_no_se_toca(sesion, alm):
    ajeno = cambio(alm, 2, "XL", 5)
    ajeno["producto"] = 1  # la fila es del modelo 2, el cambio dice modelo 1

    r = sesion.post("/admin/api/stock/aplicar", json={"cambios": [ajeno]}, headers=H)

    assert r.status_code == 409
    assert hay(alm, 2, "XL") == 0


def test_un_talle_nuevo_se_crea_solo_en_el_mayorista(sesion, tienda, alm, memoria):
    r = sesion.post("/admin/api/stock/aplicar", json={"cambios": [{"tipo": "talle_nuevo", "producto": 1, "talle": "XXL", "despues": 3}]}, headers=H)

    assert r.status_code == 200
    assert r.json()["creados"] == 1
    assert hay(alm, 1, "XXL") == 3
    assert len(tienda.productos[1]["variants"]) == 3, "la tienda no recibe el talle"
    assert memoria.filas[0]["accion"] == "talle_nuevo"
    assert memoria.filas[0]["variante_id"] == fila(alm, 1, "XXL")["id"]


def test_un_talle_que_ya_existe_no_se_duplica(sesion):
    r = sesion.post("/admin/api/stock/aplicar", json={"cambios": [{"tipo": "talle_nuevo", "producto": 1, "talle": " m ", "despues": 3}]}, headers=H)

    assert r.status_code == 409


def test_no_se_crean_talles_en_un_modelo_que_no_esta_en_el_mayorista(sesion, alm):
    r = sesion.post("/admin/api/stock/aplicar", json={"cambios": [{"tipo": "talle_nuevo", "producto": 3, "talle": "M", "despues": 3}]}, headers=H)

    assert r.status_code == 409
    assert alm.filas([3]) == []


# -- Deshacer -----------------------------------------------------------------


def test_deshacer_devuelve_el_stock_de_antes(sesion, alm):
    lote = sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(alm, 1, "M", 7), cambio(alm, 1, "L", 9)]}, headers=H).json()["lote"]

    r = sesion.post(f"/admin/api/historial/{lote}/deshacer", headers=H)

    assert r.status_code == 200
    assert (hay(alm, 1, "M"), hay(alm, 1, "L")) == (2, 3)


def test_no_se_deshace_dos_veces(sesion, alm):
    lote = sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(alm, 1, "M", 7)]}, headers=H).json()["lote"]
    sesion.post(f"/admin/api/historial/{lote}/deshacer", headers=H)

    assert sesion.post(f"/admin/api/historial/{lote}/deshacer", headers=H).status_code == 404


def test_si_el_stock_cambio_despues_no_se_deshace(sesion, alm):
    """Deshacer un guardado de ayer no puede pisar los pedidos de hoy."""
    lote = sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(alm, 1, "M", 7)]}, headers=H).json()["lote"]
    pedido(alm, "A-AAAAA", (1, "M", 1))
    alm.confirmar("A-AAAAA")

    r = sesion.post(f"/admin/api/historial/{lote}/deshacer", headers=H)

    assert r.status_code == 409
    assert hay(alm, 1, "M") == 6


def test_deshacer_un_talle_nuevo_lo_deja_en_cero(sesion, alm):
    lote = sesion.post("/admin/api/stock/aplicar", json={"cambios": [{"tipo": "talle_nuevo", "producto": 1, "talle": "XXL", "despues": 3}]}, headers=H).json()["lote"]

    sesion.post(f"/admin/api/historial/{lote}/deshacer", headers=H)

    assert hay(alm, 1, "XXL") == 0


def test_el_historial_agrupa_por_guardado(sesion, alm):
    sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(alm, 1, "M", 7), cambio(alm, 1, "L", 9)]}, headers=H)

    lotes = sesion.get("/admin/api/historial").json()

    assert len(lotes) == 1 and len(lotes[0]["entradas"]) == 2
    assert lotes[0]["se_puede_deshacer"] is True


# -- Pedidos ------------------------------------------------------------------


def test_los_pedidos_se_listan_con_lo_que_hay_ahora(sesion, alm):
    pedido(alm, "A-AAAAA", (1, "M", 2), (1, "L", 5))

    r = sesion.get("/admin/api/pedidos").json()

    p = r["pedidos"][0]
    assert (p["codigo"], p["estado"], p["unidades"], p["nombre"], p["negocio"]) == ("A-AAAAA", "nuevo", 7, "Pablo", "Kiosco")
    assert [(i["talle"], i["cantidad"], i["hay"]) for i in p["items"]] == [("M", 2, 2), ("L", 5, 3)]
    assert p["falta"] is True, "pidió 5 de una L y hay 3"
    assert r["cuenta"] == {"nuevo": 1}


def test_un_estado_desconocido_no_se_acepta(sesion):
    assert sesion.get("/admin/api/pedidos?estado=cualquiera").status_code == 422


def test_confirmar_descuenta_el_stock_y_lo_anota(sesion, alm, memoria):
    pedido(alm, "A-AAAAA", (1, "M", 2), (1, "L", 1))

    r = sesion.post("/admin/api/pedidos/A-AAAAA/confirmar", headers=H)

    assert r.status_code == 200
    assert r.json() == {"codigo": "A-AAAAA", "estado": "confirmado", "unidades": 3}
    assert (hay(alm, 1, "M"), hay(alm, 1, "L")) == (0, 2)
    assert alm.pedido("A-AAAAA")["estado"] == "confirmado"
    assert {(e["accion"], e["talle"], e["antes"], e["despues"]) for e in memoria.filas} == {("pedido", "M", 2, 0), ("pedido", "L", 3, 2)}
    assert "A-AAAAA" in memoria.filas[0]["producto"]


def test_si_no_alcanza_no_se_descuenta_nada_ni_de_lo_que_si_habia(sesion, alm, memoria):
    pedido(alm, "A-AAAAA", (1, "M", 1), (1, "L", 5))  # de la L hay 3

    r = sesion.post("/admin/api/pedidos/A-AAAAA/confirmar", headers=H)

    assert r.status_code == 409
    assert r.json()["detail"]["conflictos"][0]["talle"] == "L"
    assert (hay(alm, 1, "M"), hay(alm, 1, "L")) == (2, 3)
    assert alm.pedido("A-AAAAA")["estado"] == "nuevo"
    assert memoria.filas == []


def test_las_lineas_repetidas_del_mismo_talle_se_suman_antes_de_confirmar(sesion, alm):
    pedido(alm, "A-AAAAA", (1, "M", 2), (1, "m", 1))  # 3 en total y hay 2

    assert sesion.post("/admin/api/pedidos/A-AAAAA/confirmar", headers=H).status_code == 409
    assert hay(alm, 1, "M") == 2


def test_un_pedido_no_se_confirma_dos_veces(sesion, alm):
    pedido(alm, "A-AAAAA", (1, "M", 1))
    sesion.post("/admin/api/pedidos/A-AAAAA/confirmar", headers=H)

    r = sesion.post("/admin/api/pedidos/A-AAAAA/confirmar", headers=H)

    assert r.status_code == 409
    assert hay(alm, 1, "M") == 1, "no se descontó de nuevo"


def test_confirmar_un_pedido_que_no_existe_da_404(sesion):
    assert sesion.post("/admin/api/pedidos/A-ZZZZZ/confirmar", headers=H).status_code == 404


def test_cancelar_un_pedido_confirmado_devuelve_el_stock(sesion, alm, memoria):
    pedido(alm, "A-AAAAA", (1, "M", 2))
    sesion.post("/admin/api/pedidos/A-AAAAA/confirmar", headers=H)

    r = sesion.post("/admin/api/pedidos/A-AAAAA/cancelar", headers=H)

    assert r.status_code == 200 and r.json()["devuelto"] is True
    assert hay(alm, 1, "M") == 2
    assert alm.pedido("A-AAAAA")["estado"] == "cancelado"
    assert memoria.filas[-1]["accion"] == "pedido_cancelado"


def test_cancelar_un_pedido_nuevo_no_toca_el_stock(sesion, alm):
    pedido(alm, "A-AAAAA", (1, "M", 2))

    r = sesion.post("/admin/api/pedidos/A-AAAAA/cancelar", headers=H)

    assert r.status_code == 200 and r.json()["devuelto"] is False
    assert hay(alm, 1, "M") == 2


def test_un_pedido_cancelado_no_se_puede_confirmar(sesion, alm):
    pedido(alm, "A-AAAAA", (1, "M", 1))
    sesion.post("/admin/api/pedidos/A-AAAAA/cancelar", headers=H)

    assert sesion.post("/admin/api/pedidos/A-AAAAA/confirmar", headers=H).status_code == 409
    assert hay(alm, 1, "M") == 2


def test_solo_se_despacha_un_pedido_confirmado(sesion, alm):
    pedido(alm, "A-AAAAA", (1, "M", 1))

    assert sesion.post("/admin/api/pedidos/A-AAAAA/despachar", headers=H).status_code == 409
    sesion.post("/admin/api/pedidos/A-AAAAA/confirmar", headers=H)
    assert sesion.post("/admin/api/pedidos/A-AAAAA/despachar", headers=H).json()["estado"] == "despachado"
    assert sesion.post("/admin/api/pedidos/A-AAAAA/cancelar", headers=H).status_code == 409, "uno despachado ya no se cancela acá"


def test_los_pedidos_se_filtran_por_estado(sesion, alm):
    pedido(alm, "A-AAAAA", (1, "M", 1))
    pedido(alm, "A-BBBBB", (1, "L", 1), estado="confirmado")

    assert [p["codigo"] for p in sesion.get("/admin/api/pedidos?estado=nuevo").json()["pedidos"]] == ["A-AAAAA"]
    assert [p["codigo"] for p in sesion.get("/admin/api/pedidos?estado=confirmado").json()["pedidos"]] == ["A-BBBBB"]
    assert len(sesion.get("/admin/api/pedidos?estado=todos").json()["pedidos"]) == 2


# -- Modelos de la tienda -----------------------------------------------------


def test_los_modelos_nuevos_son_los_de_la_tienda_que_el_mayorista_no_tiene(sesion):
    r = sesion.get("/admin/api/tienda/nuevos").json()

    assert [n["id"] for n in r["nuevos"]] == [3], "el 4 no tiene foto y el 2 ya está incluido"
    assert r["nuevos"][0]["unidades"] == 5
    assert [(t["talle"], t["stock"]) for t in r["nuevos"][0]["talles"]] == [("S", 1), ("M", 4)]


def test_incluir_un_modelo_con_el_stock_en_cero(sesion, alm, invalidaciones, memoria):
    r = sesion.post("/admin/api/tienda/incluir", json={"producto": 3, "copiar_stock": False}, headers=H)

    assert r.status_code == 200
    assert [(f["talle"], f["cantidad"]) for f in alm.filas([3])] == [("S", 0), ("M", 0)]
    assert invalidaciones, "la web tiene que volver a leer los nombres y fotos"
    assert memoria.filas[0]["accion"] == "incluido"


def test_incluir_un_modelo_copiando_el_stock_de_la_tienda(sesion, alm):
    sesion.post("/admin/api/tienda/incluir", json={"producto": 3, "copiar_stock": True}, headers=H)

    assert [(f["talle"], f["cantidad"]) for f in alm.filas([3])] == [("S", 1), ("M", 4)]


def test_incluir_no_toca_la_tienda_ni_se_vuelve_a_copiar_solo(sesion, tienda, alm):
    sesion.post("/admin/api/tienda/incluir", json={"producto": 3, "copiar_stock": True}, headers=H)
    tienda.vender(3, "M", 4)

    assert tienda.stock(3, "M") == 0
    assert hay(alm, 3, "M") == 4, "después de incluirlo, el stock del mayorista es solo suyo"


def test_un_modelo_no_se_incluye_dos_veces(sesion):
    sesion.post("/admin/api/tienda/incluir", json={"producto": 3}, headers=H)

    assert sesion.post("/admin/api/tienda/incluir", json={"producto": 3}, headers=H).status_code == 409


@pytest.mark.parametrize("producto", [2, 4, 999])
def test_no_se_incluye_lo_que_no_esta_publicado_o_no_tiene_foto(sesion, producto):
    r = sesion.post("/admin/api/tienda/incluir", json={"producto": producto}, headers=H)

    assert r.status_code in (409, 422)


def test_la_carga_inicial_solo_corre_con_el_stock_vacio(sesion):
    r = sesion.post("/admin/api/tienda/cargar-stock", headers=H)

    assert r.status_code == 409


def test_la_carga_inicial_copia_lo_elegible_de_la_tienda(monkeypatch, tienda, memoria, invalidaciones):
    vacio = almacen.AlmacenMemoria()
    monkeypatch.setattr(acceso, "USUARIO", "leo")
    monkeypatch.setattr(acceso, "CLAVE", CLAVE)
    monkeypatch.setattr(acceso, "SECRETO", "s" * 40)
    acceso._fallos.clear()
    app = FastAPI()
    app.include_router(admin.crear_router(memoria, lambda: invalidaciones.append(1), vacio))
    c = TestClient(app)
    c.post("/admin/api/login", json={"usuario": "leo", "clave": CLAVE}, headers=H)

    assert c.get("/admin/api/stock").json()["vacio"] is True
    r = c.post("/admin/api/tienda/cargar-stock", headers=H)

    assert r.status_code == 200
    assert r.json()["modelos"] == 2, "el 1 y el 3: el 2 está oculto y el 4 no tiene foto"
    assert hay(vacio, 1, "M") == 2 and hay(vacio, 3, "M") == 4
    assert vacio.filas([2]) == [] and vacio.filas([4]) == []


def test_las_filas_iniciales_dejan_afuera_lo_que_no_se_puede_ofrecer(tienda):
    sin_control = tienda.productos[1]
    sin_control["variants"][0]["stock"] = None  # la S no lleva cuenta en la tienda

    filas = admin.filas_iniciales(list(tienda.productos.values()))

    assert (1, "S", 0) not in filas
    assert (1, "M", 2) in filas
    assert not [f for f in filas if f[0] in (2, 4)]


# -- La página del panel -------------------------------------------------------


def test_la_pagina_del_panel_se_sirve_y_no_se_indexa():
    sys.modules.pop("sitio", None)
    import sitio as servidor

    c = TestClient(servidor.app)
    r = c.get("/admin/")

    assert r.status_code == 200
    assert "Panel mayorista" in r.text
    assert "noindex" in r.text
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-frame-options"] == "DENY"


def test_sin_configurar_el_panel_responde_503_y_no_deja_pasar():
    sys.modules.pop("sitio", None)
    import sitio as servidor

    c = TestClient(servidor.app)

    assert c.get("/admin/api/estado").json()["configurado"] is False
    assert c.get("/admin/api/stock").status_code == 503
