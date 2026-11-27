"""Envío aproximado en el carrito: tablas de tarifas por transporte, zona de provincias y peso.

El cálculo lo hace la web (static/index.html); el servidor guarda y valida las tablas
y solo publica los transportes que tienen algún precio cargado.
"""

from __future__ import annotations

import json
from pathlib import Path

from test_mayorista import guardar_web, servidor  # noqa: F401  (deja mayorista en sys.path)
from test_mayorista import cache_limpia, cliente, con_lineas, negocio_de_ejemplo, propio  # noqa: F401  (fixtures)

import ajustes  # noqa: E402

CORREO = {"nombre": "Correo Argentino", "kilos": [2, 5, 10], "zonas": [
    {"nombre": "Córdoba", "provincias": ["Córdoba"], "dias": "2 a 4 días", "precios": [6000, 8000, 11000]},
    {"nombre": "Centro", "provincias": ["Buenos Aires", "Santa Fe"], "dias": "", "precios": [0, 0, 0]},
]}
VIA_CARGO_VACIA = {"nombre": "Vía Cargo", "kilos": [2, 5], "zonas": [{"nombre": "Todo", "provincias": ["Córdoba"], "dias": "", "precios": [0, 0]}]}


def con(**cambios):
    return ajustes.AjustesWeb(**{**ajustes.por_defecto({}, "Tienda", "5491100000000", "", True, 6, []), **cambios})


def test_una_tabla_bien_armada_pasa():
    assert ajustes.problemas(con(envios=[CORREO])) == []


def test_una_provincia_no_puede_estar_en_dos_zonas():
    mal = {**CORREO, "zonas": CORREO["zonas"] + [{"nombre": "Otra", "provincias": ["Córdoba"], "dias": "", "precios": [1, 1, 1]}]}
    assert any("dos zonas" in m for m in ajustes.problemas(con(envios=[mal])))


def test_un_precio_por_columna_de_kilos():
    mal = {**CORREO, "zonas": [{"nombre": "Córdoba", "provincias": ["Córdoba"], "dias": "", "precios": [6000]}]}
    assert any("un precio por cada columna" in m for m in ajustes.problemas(con(envios=[mal])))


def test_los_kilos_van_de_menor_a_mayor_y_las_provincias_existen():
    assert any("menor a mayor" in m for m in ajustes.problemas(con(envios=[{**CORREO, "kilos": [5, 2, 10]}])))
    mal = {**CORREO, "zonas": [{"nombre": "X", "provincias": ["Narnia"], "dias": "", "precios": [1, 1, 1]}]}
    assert any("no es una provincia" in m for m in ajustes.problemas(con(envios=[mal])))


def test_el_negocio_trae_la_plantilla_con_precios_en_cero():
    negocio = json.loads((Path(servidor.__file__).parent / "negocio.json").read_text(encoding="utf-8"))
    nombres = [t["nombre"] for t in negocio["envios"]]

    assert nombres == ["Correo Argentino", "Vía Cargo"]
    assert ajustes.problemas(con(envios=negocio["envios"], pesoCamiseta=negocio["pesoCamiseta"])) == []
    cubiertas = sorted(p for z in negocio["envios"][0]["zonas"] for p in z["provincias"])
    assert cubiertas == sorted(ajustes.PROVINCIAS), "la plantilla cubre las 24 provincias"


def test_el_negocio_trae_las_tarifas_de_via_cargo_desde_morteros():
    # Cotizadas el 2026-11-26 en viacargo.com.ar, de Morteros (2421), despacho en agencia y retiro en agencia.
    negocio = json.loads((Path(servidor.__file__).parent / "negocio.json").read_text(encoding="utf-8"))
    via = next(t for t in negocio["envios"] if t["nombre"] == "Vía Cargo")
    zonas = {z["nombre"]: z for z in via["zonas"]}

    assert via["kilos"] == [2, 5, 10, 15, 20, 30, 50]
    assert zonas["Centro, Cuyo y Litoral"]["precios"][2] == 20000 and "Córdoba" in zonas["Centro, Cuyo y Litoral"]["provincias"]
    assert zonas["Patagonia"]["precios"][-1] == 116000
    # Tierra del Fuego no sale en el cotizador de Vía Cargo: queda «a cotizar».
    assert "Tierra del Fuego" not in [p for z in via["zonas"] for p in z["provincias"]]
    assert all(all(p > 0 for p in z["precios"]) for z in via["zonas"])
    assert negocio["enviosActualizados"] == "2026-11-26"


def test_la_web_recibe_solo_los_transportes_con_precio(cliente, con_lineas):
    guardar_web(con_lineas, envios=[CORREO, VIA_CARGO_VACIA], pesoCamiseta=300)
    d = cliente.get("/api/catalogo").json()

    assert [t["nombre"] for t in d["envios"]] == ["Correo Argentino"]
    assert d["pesoCamiseta"] == 300
    assert "Córdoba" in d["provincias"] and len(d["provincias"]) == 24


def test_sin_precios_cargados_la_web_no_muestra_envios(cliente, con_lineas):
    guardar_web(con_lineas, envios=[VIA_CARGO_VACIA])

    assert cliente.get("/api/catalogo").json()["envios"] == []


def test_la_web_recibe_el_paquete_y_el_divisor(cliente, con_lineas):
    guardar_web(con_lineas, envios=[{**CORREO, "divisor": 4000}], paqueteLargo=40, paqueteAncho=30, altoPorCamiseta=15)
    d = cliente.get("/api/catalogo").json()

    assert d["paquete"] == {"largo": 40, "ancho": 30, "altoMm": 15}
    assert d["envios"][0]["divisor"] == 4000


def test_el_negocio_trae_el_paquete_de_30x40_y_divisor():
    negocio = json.loads((Path(servidor.__file__).parent / "negocio.json").read_text(encoding="utf-8"))

    assert (negocio["paqueteLargo"], negocio["paqueteAncho"]) == (40, 30)
    assert all(t["divisor"] == 5000 for t in negocio["envios"])


def test_la_fecha_de_las_tarifas_tiene_que_ser_una_fecha():
    import pydantic
    import pytest

    assert con(enviosActualizados="2026-11-26").enviosActualizados == "2026-11-26"
    with pytest.raises(pydantic.ValidationError):
        con(enviosActualizados="ayer")
