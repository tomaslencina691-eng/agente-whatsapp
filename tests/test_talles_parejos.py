"""El pedido parejo por talle y las metas de la barra para pedidos grandes.

  · Dentro de cada línea (calidad), un mismo talle puede ser hasta el tope % de las
    camisetas de esa línea, con un piso de 2. Los modelos se mezclan: cuenta el talle.
  · La web no deja enviar un pedido desparejo (static/index.html repite la cuenta);
    si igual llega uno, se guarda marcado para que el panel lo muestre.
"""

from __future__ import annotations

import pytest

from test_mayorista import LINEA_G5, LINEA_TOP, guardar_web, pedido_web, servidor  # noqa: F401  (deja mayorista en sys.path)
from test_mayorista import cache_limpia, cliente, con_lineas, negocio_de_ejemplo, propio  # noqa: F401  (fixtures)

import ajustes  # noqa: E402
import precios  # noqa: E402

LINEAS = [{"id": "g5", "nombre": "G5", "cats": ["g5"], "minimo": 6, "tramos": []},
          {"id": "top", "nombre": "Top Quality", "cats": ["jugador"], "minimo": 6, "tramos": []},
          {"id": "top_pers", "nombre": "Top con parche", "cats": [], "personalizadaDe": "top", "minimo": 6, "tramos": []}]


@pytest.mark.parametrize("total, tope, maximo", [(6, 40, 2), (12, 40, 4), (100, 40, 40), (6, 30, 2), (12, 30, 3), (3, 40, 2)])
def test_cuantas_de_un_talle_entran(total, tope, maximo):
    assert precios.maximo_por_talle(total, tope) == maximo


def test_el_talle_base_ignora_nombre_y_numero():
    assert precios.talle_base("L, Di María 22") == "L"
    assert precios.talle_base("2xl") == "XXL"
    assert precios.talle_base(" m ") == "M"


def test_doce_l_de_g5_no_es_parejo():
    malos = precios.talles_desparejos([(["g5"], "L", 12)], LINEAS, 40)

    assert malos == [{"linea": "G5", "talle": "L", "cantidad": 12, "maximo": 4, "total": 12}]


def test_una_curva_pareja_pasa_aunque_mezcle_modelos():
    items = [(["g5"], "M", 2), (["g5"], "L", 2), (["g5"], "L, Messi 10", 1), (["g5"], "XL", 3), (["g5"], "XXL", 4)]

    assert precios.talles_desparejos(items, LINEAS, 40) == [], "12 G5: 2 M, 3 L, 3 XL, 4 XXL (hasta 4 por talle)"


def test_cada_calidad_se_cuenta_aparte():
    # 6 L de G5 y 6 de Top Quality repartidas: la G5 se pasa (máximo 2), la Top no.
    items = [(["g5"], "L", 6), (["jugador"], "M", 2), (["jugador"], "L", 2), (["jugador"], "XL", 2)]

    malos = precios.talles_desparejos(items, LINEAS, 40)

    assert [(m["linea"], m["talle"]) for m in malos] == [("G5", "L")]


def test_las_con_parche_son_otra_calidad():
    items = [(["jugador"], "L", 2), (["jugador"], "M", 2), (["jugador"], "XL", 2), (["jugador"], "L", 6, True)]

    malos = precios.talles_desparejos(items, LINEAS, 40)

    assert [(m["linea"], m["cantidad"]) for m in malos] == [("Top con parche", 6)]


def test_sin_tope_no_hay_regla():
    assert precios.talles_desparejos([(["g5"], "L", 12)], LINEAS, 0) == []


def test_los_ajustes_validan_el_tope_y_las_metas():
    base = ajustes.AjustesWeb(**{**ajustes.por_defecto({}, "Tienda", "5491100000000", "", True, 6, []), "topeTalle": 10, "hitos": [50, 50]})

    malos = ajustes.problemas(base)

    assert any("20 %" in m for m in malos) and any("metas" in m for m in malos)


def test_el_negocio_trae_el_tope_y_las_metas():
    d = ajustes.por_defecto({"topeTalle": 40, "hitos": [200, 50, 100]}, "Tienda", "5491100000000", "", True, 6, [])

    assert (d["topeTalle"], d["hitos"]) == (40, [50, 100, 200])


# -- Por la web -----------------------------------------------------------------------


def test_la_web_recibe_el_tope_y_las_metas(cliente, con_lineas):
    guardar_web(con_lineas, lineas=[LINEA_TOP, LINEA_G5], topeTalle=40, hitos=[50, 100, 200])

    d = cliente.get("/api/catalogo").json()

    assert (d["topeTalle"], d["hitos"]) == (40, [50, 100, 200])


def test_un_pedido_desparejo_se_guarda_marcado(cliente, con_lineas):
    guardar_web(con_lineas, lineas=[LINEA_TOP, LINEA_G5], topeTalle=40)
    cliente.get("/api/catalogo")  # trae los modelos

    cliente.post("/api/pedido", json=pedido_web(items=[{"producto": 1, "talle": "M", "cantidad": 8}]))

    marcado = con_lineas.pedido("A-7K3QX")["datos"]["tallesDesparejos"]
    assert [(m["talle"], m["cantidad"], m["maximo"]) for m in marcado] == [("M", 8, 3)]


def test_un_pedido_parejo_no_lleva_marca(cliente, con_lineas):
    guardar_web(con_lineas, lineas=[LINEA_TOP, LINEA_G5], topeTalle=40)
    cliente.get("/api/catalogo")

    cliente.post("/api/pedido", json=pedido_web(items=[{"producto": 1, "talle": "M", "cantidad": 2}]))

    assert "tallesDesparejos" not in con_lineas.pedido("A-7K3QX")["datos"]


# -- Los datos para transferir -----------------------------------------------------------


def test_la_web_recibe_los_datos_para_transferir(cliente, con_lineas):
    guardar_web(con_lineas, aliasPago="Tomi.lencin", titularPago="Tomás Geremias Lencina", cuentaPago="Lemon")

    d = cliente.get("/api/catalogo").json()

    assert (d["aliasPago"], d["titularPago"], d["cuentaPago"]) == ("Tomi.lencin", "Tomás Geremias Lencina", "Lemon")


def test_el_alias_sale_de_negocio_json_si_no_se_guardo_otro():
    d = ajustes.por_defecto({"aliasPago": "Tomi.lencin", "cuentaPago": "Lemon"}, "Tienda", "5491100000000", "", True, 6, [])

    assert (d["aliasPago"], d["titularPago"], d["cuentaPago"]) == ("Tomi.lencin", "", "Lemon")
