"""Pruebas de los ajustes de la web (ajustes.py). No usan base."""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mayorista"))

import ajustes  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20
JPG = b"\xff\xd8\xff" + b"0" * 20
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"0" * 20


def base_de_ejemplo(**cambios):
    d = ajustes.por_defecto({}, "Mi Tienda", "5491100000000", "", True, 6, [{"desde": 12, "precio": 25000}, {"desde": 6, "precio": 28000}])
    d.pop("logoHash")
    return ajustes.AjustesWeb(**{**d, **cambios})


def test_los_ajustes_de_siempre_salen_de_negocio_json_y_del_entorno():
    d = ajustes.por_defecto({"filtroVersion": False, "condiciones": "Sin comprobante no se arma", "reglas": [{"etiqueta": "Envío", "titulo": "Vía Cargo", "texto": "x"}]},
                            "SF", "549", "https://t.example", False, 6, [{"desde": 12, "precio": 1}, {"desde": 6, "precio": 2}])

    assert (d["marca"], d["whatsapp"], d["limitarStock"], d["filtroVersion"], d["condiciones"]) == ("SF", "549", False, False, "Sin comprobante no se arma")
    assert [t["desde"] for t in d["tramos"]] == [6, 12]
    assert d["reglas"][0]["titulo"] == "Vía Cargo"


def test_lo_guardado_pisa_lo_de_siempre_y_lo_demas_queda():
    base = {"marca": "A", "minimo": 6}

    assert ajustes.combinar(base, {"marca": "B"}) == {"marca": "B", "minimo": 6}
    assert ajustes.combinar(base, None) == base
    assert ajustes.combinar(base, {"viejo": 1}) == base


LINEA_G5 = {"id": "g5", "nombre": "G5", "cats": ["g5"], "minimo": 6, "tramos": [{"desde": 12, "precio": 26500}, {"desde": 6, "precio": 33500}]}
LINEA_TOP = {"id": "top", "nombre": "Top Quality", "cats": ["jugador"], "minimo": 6, "tramos": [{"desde": 6, "precio": 35000}]}


def test_las_lineas_de_precios_salen_de_negocio_json_con_las_escalas_de_menor_a_mayor():
    d = ajustes.por_defecto({"lineas": [LINEA_G5]}, "SF", "549", "", True, 6, [])

    assert d["lineas"] == [{"id": "g5", "nombre": "G5", "cats": ["g5"], "minimo": 6, "tramos": [{"desde": 6, "precio": 33500}, {"desde": 12, "precio": 26500}]}]


def test_sin_lineas_en_negocio_json_no_hay_lineas():
    assert ajustes.por_defecto({}, "SF", "549", "", True, 6, [])["lineas"] == []


def test_una_linea_sin_minimo_propio_toma_el_general():
    sin_minimo = {k: v for k, v in LINEA_G5.items() if k != "minimo"}

    assert ajustes.por_defecto({"lineas": [sin_minimo]}, "SF", "549", "", True, 9, [])["lineas"][0]["minimo"] == 9


def test_lineas_validas_no_tienen_problemas():
    assert ajustes.problemas(base_de_ejemplo(lineas=[LINEA_G5, LINEA_TOP])) == []


def test_dos_lineas_con_el_mismo_identificador_son_un_problema():
    assert any("mismo identificador" in p for p in ajustes.problemas(base_de_ejemplo(lineas=[LINEA_G5, {**LINEA_TOP, "id": "g5"}])))


def test_una_linea_con_dos_escalas_desde_la_misma_cantidad_es_un_problema():
    repetida = {**LINEA_G5, "tramos": [{"desde": 6, "precio": 1}, {"desde": 6, "precio": 2}]}

    assert any("G5" in p and "misma cantidad" in p for p in ajustes.problemas(base_de_ejemplo(lineas=[repetida])))


def test_una_categoria_en_dos_lineas_es_un_problema():
    assert any("g5" in p for p in ajustes.problemas(base_de_ejemplo(lineas=[LINEA_G5, {**LINEA_TOP, "cats": ["jugador", "g5"]}])))


def test_ajustes_validos_no_tienen_problemas():
    assert ajustes.problemas(base_de_ejemplo()) == []


def test_el_mensaje_sin_pedido_es_un_problema():
    assert any("{pedido}" in p for p in ajustes.problemas(base_de_ejemplo(mensaje="Hola")))


def test_un_dato_desconocido_en_el_mensaje_es_un_problema():
    assert any("{inventado}" in p for p in ajustes.problemas(base_de_ejemplo(mensaje="{pedido} {inventado}")))


@pytest.mark.parametrize("crudo, tipo", [(PNG, "image/png"), (JPG, "image/jpeg"), (WEBP, "image/webp")])
def test_logos_validos(crudo, tipo):
    assert ajustes.leer_logo(base64.b64encode(crudo).decode()) == (crudo, tipo)
    assert ajustes.leer_logo("data:image/x;base64," + base64.b64encode(crudo).decode()) == (crudo, tipo)


@pytest.mark.parametrize(
    "datos",
    [
        pytest.param("???", id="no-es-base64"),
        pytest.param(base64.b64encode(b"texto").decode(), id="no-es-imagen"),
        pytest.param(base64.b64encode(PNG + b"0" * ajustes.PESO_MAXIMO_DEL_LOGO).decode(), id="pesa-mas-de-1mb"),
        pytest.param(base64.b64encode(b"GIF89a" + b"0" * 20).decode(), id="gif"),
    ],
)
def test_logos_que_no_sirven(datos):
    with pytest.raises(ValueError):
        ajustes.leer_logo(datos)


def test_la_huella_del_logo_cambia_con_la_imagen():
    assert ajustes.huella(PNG) != ajustes.huella(JPG)
    assert ajustes.huella(PNG) == ajustes.huella(PNG)
