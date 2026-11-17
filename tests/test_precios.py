"""Pruebas del precio por línea de producto (precios.py). No usan base ni red."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mayorista"))

import precios  # noqa: E402

TOP = {"id": "top", "nombre": "Top Quality", "cats": ["jugador", "retro"], "minimo": 6, "tramos": [{"desde": 6, "precio": 35000}, {"desde": 12, "precio": 30000}]}
G5 = {"id": "g5", "nombre": "G5", "cats": ["g5", "fan"], "minimo": 6, "tramos": [{"desde": 6, "precio": 33500}, {"desde": 12, "precio": 26500}]}
SHORTS = {"id": "shorts", "nombre": "Shorts", "cats": ["shorts"], "minimo": 6, "tramos": [{"desde": 6, "precio": 35000}, {"desde": 12, "precio": 33000}]}
LINEAS = [TOP, G5, SHORTS]


def sin_escala_general(_unidades: int) -> int | None:
    return None


def test_un_modelo_cae_en_la_primera_linea_que_incluye_alguna_de_sus_categorias():
    assert precios.linea_de(["g5"], LINEAS)["id"] == "g5"
    assert precios.linea_de(["fan", "jugador"], LINEAS)["id"] == "top", "gana la línea que está primero en el panel"
    assert precios.linea_de(["retro", "g5"], LINEAS)["id"] == "top"


def test_un_modelo_sin_categoria_conocida_no_tiene_linea():
    assert precios.linea_de([], LINEAS) is None
    assert precios.linea_de(["otra"], LINEAS) is None


@pytest.mark.parametrize("unidades, esperado", [(5, None), (6, 33500), (11, 33500), (12, 26500), (40, 26500)])
def test_el_precio_sale_de_la_escala_que_alcanza(unidades, esperado):
    assert precios.precio_de_linea(G5, unidades) == esperado


def test_seis_y_doce_de_cada_linea_con_su_precio():
    assert precios.total_del_pedido([(["g5"], 6)], LINEAS, sin_escala_general) == 6 * 33500
    assert precios.total_del_pedido([(["g5"], 12)], LINEAS, sin_escala_general) == 12 * 26500
    assert precios.total_del_pedido([(["jugador"], 12)], LINEAS, sin_escala_general) == 12 * 30000
    assert precios.total_del_pedido([(["shorts"], 6)], LINEAS, sin_escala_general) == 6 * 35000
    assert precios.total_del_pedido([(["shorts"], 12)], LINEAS, sin_escala_general) == 12 * 33000


def test_los_modelos_de_una_misma_linea_se_suman_para_llegar_a_la_escala():
    # 4 de un equipo y 8 de otro, los dos G5: son 12 G5.
    assert precios.total_del_pedido([(["g5"], 4), (["g5"], 8)], LINEAS, sin_escala_general) == 12 * 26500


def test_las_calidades_no_se_mezclan_para_llegar_a_una_escala():
    # 6 G5 + 6 Top Quality: cada línea paga su escala de 6, no la de 12.
    esperado = 6 * 33500 + 6 * 35000
    assert precios.total_del_pedido([(["g5"], 6), (["jugador"], 6)], LINEAS, sin_escala_general) == esperado


def test_una_linea_que_no_llega_al_minimo_deja_el_total_a_confirmar():
    assert precios.total_del_pedido([(["g5"], 6), (["jugador"], 3)], LINEAS, sin_escala_general) is None


def test_lo_que_no_tiene_linea_usa_la_escala_general():
    general = lambda n: 28000 if n >= 6 else None  # noqa: E731
    assert precios.total_del_pedido([(["otra"], 6), (["g5"], 6)], LINEAS, general) == 6 * 28000 + 6 * 33500


def test_lo_que_no_tiene_linea_ni_escala_general_se_confirma_con_una_persona():
    assert precios.total_del_pedido([(["otra"], 6), (["g5"], 6)], LINEAS, sin_escala_general) is None


def test_un_pedido_vacio_no_cuesta_nada():
    assert precios.total_del_pedido([], LINEAS, sin_escala_general) == 0


TOP_PERS = {"id": "top_pers", "nombre": "Top Quality con parche y número", "cats": [], "personalizadaDe": "top", "minimo": 6, "tramos": [{"desde": 6, "precio": 45000}, {"desde": 12, "precio": 42000}]}
LINEAS_PERS = [TOP, TOP_PERS, G5, SHORTS]


def test_una_camiseta_con_parche_y_numero_va_a_la_variante_de_su_linea():
    assert precios.variante_de(TOP, LINEAS_PERS)["id"] == "top_pers"
    assert precios.variante_de(G5, LINEAS_PERS)["id"] == "g5", "sin variante sigue en su línea"


def test_las_lisas_y_las_de_parche_y_numero_se_cuentan_aparte():
    # 6 lisas a 35.000 y 6 con parche y número a 45.000: no suman 12.
    items = [(["jugador"], 6, False), (["retro"], 6, True)]
    assert precios.total_del_pedido(items, LINEAS_PERS, sin_escala_general) == 6 * 35000 + 6 * 45000


def test_doce_con_parche_y_numero_pagan_la_escala_de_doce():
    assert precios.total_del_pedido([(["jugador"], 5, True), (["retro"], 7, True)], LINEAS_PERS, sin_escala_general) == 12 * 42000


def test_la_variante_tambien_tiene_que_llegar_a_su_minimo():
    assert precios.total_del_pedido([(["jugador"], 6, False), (["jugador"], 2, True)], LINEAS_PERS, sin_escala_general) is None


def test_g5_tiene_un_tercer_precio_desde_40():
    g5 = {**G5, "tramos": [{"desde": 6, "precio": 33500}, {"desde": 12, "precio": 26000}, {"desde": 40, "precio": 25500}]}
    assert precios.precio_de_linea(g5, 39) == 26000
    assert precios.precio_de_linea(g5, 40) == 25500
