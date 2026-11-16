"""Pruebas del almacén en memoria (la misma interfaz que Postgres). No usan base."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mayorista"))

import almacen  # noqa: E402


@pytest.fixture
def alm():
    return almacen.AlmacenMemoria()


def test_ignorar_dos_veces_no_duplica(alm):
    alm.ignorar(3, "River")
    alm.ignorar(3, "River")

    assert [i["producto_id"] for i in alm.ignorados()] == [3]


def test_volver_a_considerar_saca_al_modelo_y_no_falla_si_no_estaba(alm):
    alm.ignorar(3, "River")
    alm.designorar(3)
    alm.designorar(99)

    assert alm.ignorados() == []


def test_lo_ultimo_ignorado_sale_primero(alm):
    alm.ignorar(1, "Uno")
    alm.ignorar(2, "Dos")

    assert [i["producto_id"] for i in alm.ignorados()] == [2, 1]


def test_un_ajuste_sin_guardar_es_none_y_uno_vacio_no(alm):
    assert alm.config("categorias") is None

    alm.guardar_config("categorias", [])

    assert alm.config("categorias") == []


def test_los_ajustes_se_guardan_y_se_pisan(alm):
    alm.guardar_config("categorias", [{"id": "a"}])
    alm.guardar_config("categorias", [{"id": "b"}])

    assert alm.config("categorias") == [{"id": "b"}]


def test_un_ajuste_leido_no_se_puede_cambiar_por_fuera(alm):
    alm.guardar_config("categorias", [{"id": "a"}])

    alm.config("categorias")[0]["id"] = "roto"

    assert alm.config("categorias") == [{"id": "a"}]


def test_las_lineas_repetidas_se_juntan_sin_importar_mayusculas_ni_espacios():
    items = [{"producto": 1, "talle": "m", "cantidad": 2}, {"producto": 1, "talle": " M ", "cantidad": 1}, {"producto": 1, "talle": "L", "cantidad": 4}]

    juntos = almacen.agrupar_items(items)

    assert [(i["talle"], i["cantidad"]) for i in juntos] == [("m", 3), ("L", 4)]
