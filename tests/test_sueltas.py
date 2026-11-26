"""Regla nueva: con 6 Top Quality lisas, de 1 a 11 Top Quality con dorsal a $45.000.

La variante «con parche y número» tiene mínimo 6 como cualquier línea; si su línea base
(la lisa) ya llegó a su mínimo, se puede sumar desde 1, al precio de su primera escala.
Desde 12 sigue su escala normal ($42.000). La web repite la cuenta en static/index.html.
"""

from __future__ import annotations

import pytest

from test_mayorista import LINEA_G5, LINEA_TOP, LINEA_TOP_PERS, guardar_web, pedido_web, servidor  # noqa: F401
from test_mayorista import cache_limpia, cliente, con_lineas, negocio_de_ejemplo, propio  # noqa: F401  (fixtures)

import ajustes  # noqa: E402
import precios  # noqa: E402

LINEAS = [LINEA_TOP, LINEA_TOP_PERS, LINEA_G5]
SUELTAS = ["top_pers"]


def total(lisas, dorsal, sueltas=SUELTAS, g5=0):
    items = [(["jugador"], lisas, False), (["jugador"], dorsal, True), (["g5"], g5, False)]
    return precios.total_del_pedido([i for i in items if i[1]], LINEAS, lambda n: None, sueltas)


@pytest.mark.parametrize("dorsal", [1, 3, 11])
def test_con_6_lisas_las_dorsal_van_desde_1_a_45000(dorsal):
    assert total(6, dorsal) == 6 * 35000 + dorsal * 45000


def test_desde_12_con_dorsal_siguen_su_escala():
    assert total(6, 12) == 6 * 35000 + 12 * 42000


def test_sin_6_lisas_las_dorsal_necesitan_su_minimo():
    assert total(5, 3) is None
    assert total(0, 3) is None
    assert total(5, 6) is None, "5 lisas no llegan a su mínimo"


def test_las_g5_no_habilitan_las_dorsal_de_top():
    assert total(0, 3, g5=6) is None


def test_sin_la_regla_prendida_no_hay_sueltas():
    assert total(6, 3, sueltas=[]) is None


def test_que_variantes_quedan_habilitadas():
    assert precios.sueltas_habilitadas({"top": 6}, LINEAS, SUELTAS) == {"top_pers"}
    assert precios.sueltas_habilitadas({"top": 5}, LINEAS, SUELTAS) == set()
    assert precios.sueltas_habilitadas({"top": 6}, LINEAS, ["top"]) == set(), "solo una variante puede ir suelta"


def test_los_ajustes_solo_aceptan_variantes():
    base = ajustes.por_defecto({}, "Tienda", "5491100000000", "", True, 6, [])
    bien = ajustes.AjustesWeb(**{**base, "lineas": LINEAS, "variantesSueltas": ["top_pers"]})
    mal = ajustes.AjustesWeb(**{**base, "lineas": LINEAS, "variantesSueltas": ["top"]})

    assert not any("suelt" in m.lower() for m in ajustes.problemas(bien))
    assert ajustes.problemas(mal)


def test_el_negocio_trae_la_regla_prendida():
    import json
    from pathlib import Path

    negocio = json.loads((Path(servidor.__file__).parent / "negocio.json").read_text(encoding="utf-8"))
    assert negocio["variantesSueltas"] == ["top_pers"]


def test_el_pedido_con_6_lisas_y_3_dorsal_se_cobra_a_45000(cliente, con_lineas):
    guardar_web(con_lineas, lineas=LINEAS, tramos=[], variantesSueltas=SUELTAS)
    d = cliente.get("/api/catalogo").json()
    assert d["variantesSueltas"] == SUELTAS
    items = [{"producto": 2, "talle": "M", "cantidad": 6}, {"producto": 2, "talle": "M", "cantidad": 3, "pers": True}]

    cliente.post("/api/pedido", json=pedido_web(items=items))

    assert con_lineas.pedido("A-7K3QX")["total"] == 6 * 35000 + 3 * 45000
