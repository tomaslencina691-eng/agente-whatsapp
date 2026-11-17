"""Pruebas de las herramientas. No salen a internet ni gastan tokens.

Open-Meteo se reemplaza por datos escritos a mano: lo que se prueba es lo
nuestro (cómo se arma el texto, qué pasa cuando algo falla), no que la API
de ellos ande.

    pytest
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agente import herramientas  # noqa: E402
from agente.herramientas import HERRAMIENTAS, clima  # noqa: E402

ROSARIO = {
    "nombre": "Rosario",
    "provincia": "Provincia de Santa Fe",
    "pais": "Argentina",
    "latitud": -32.94682,
    "longitud": -60.63932,
}

MEDICION = {
    "current": {
        "time": "2026-07-28T14:30",
        "temperature_2m": 19.1,
        "relative_humidity_2m": 83,
        "apparent_temperature": 18.4,
        "weather_code": 3,
        "wind_speed_10m": 13.8,
    }
}


def sin_internet(monkeypatch, lugar=ROSARIO, medicion=MEDICION) -> None:
    """Deja la herramienta andando con datos inventados, sin tocar la red."""
    monkeypatch.setattr(herramientas, "_buscar_lugar", lambda nombre: lugar)
    monkeypatch.setattr(
        herramientas, "_pedir_el_clima", lambda latitud, longitud: medicion
    )


def test_el_clima_sale_en_castellano_y_con_los_datos(monkeypatch):
    sin_internet(monkeypatch)

    texto = clima.invoke({"lugar": "Rosario"})

    assert "Rosario, Provincia de Santa Fe, Argentina" in texto
    assert "19.1 °C" in texto
    assert "nublado" in texto, "el código 3 del WMO es cielo nublado"
    assert "83 %" in texto
    assert "13.8 km/h" in texto
    assert "14:30" in texto


def test_traduce_los_codigos_del_cielo():
    assert herramientas._describir_cielo(0) == "despejado"
    assert herramientas._describir_cielo(95) == "con tormenta"
    assert herramientas._describir_cielo(None) == "sin datos"
    # Un código que no está en la tabla no puede romper: se informa el número.
    assert "444" in herramientas._describir_cielo(444)


def test_si_falta_un_dato_no_se_rompe(monkeypatch):
    """Open-Meteo no siempre manda todo. Lo que falta se omite, no explota."""
    sin_internet(monkeypatch, medicion={"current": {"temperature_2m": 7.0}})

    texto = clima.invoke({"lugar": "Rosario"})

    assert "7.0 °C" in texto
    assert "Humedad" not in texto, "lo que no vino no se inventa ni se muestra vacío"


def test_si_no_existe_la_ciudad_lo_dice(monkeypatch):
    monkeypatch.setattr(herramientas, "_buscar_lugar", lambda nombre: None)

    texto = clima.invoke({"lugar": "Ciudad Gótica"})

    assert "Ciudad Gótica" in texto
    assert "no encontré" in texto.lower()


def test_si_se_cae_la_api_la_charla_sigue(monkeypatch):
    """Una herramienta que levanta una excepción corta toda la respuesta.

    Devolviendo el problema como texto, el modelo lo lee y se lo explica a la
    persona en vez de que la conversación se caiga con un error crudo.
    """

    def explota(nombre):
        raise TimeoutError("tardó demasiado")

    monkeypatch.setattr(herramientas, "_buscar_lugar", explota)

    texto = clima.invoke({"lugar": "Rosario"})

    assert "TimeoutError" in texto
    assert "tardó demasiado" in texto


def test_el_nombre_completo_no_deja_comas_sueltas():
    """Cuando no hay provincia, no puede quedar "Madrid, , España"."""
    solo = {"nombre": "Madrid", "provincia": "", "pais": "España"}

    assert herramientas._nombre_completo(solo) == "Madrid, España"


def test_la_herramienta_esta_en_la_lista_que_mira_el_grafo():
    """Si no está acá, el modelo no se entera de que existe."""
    assert clima in HERRAMIENTAS


def test_el_modelo_recibe_una_descripcion_util():
    """El docstring no es decorativo: es lo único que el modelo lee para
    decidir si la herramienta le sirve."""
    assert clima.name == "clima"
    assert "clima" in clima.description.lower()
    assert "lugar" in clima.args


# -- Fotos de producto --------------------------------------------------------


def test_la_foto_del_producto_va_en_una_linea_con_etiqueta():
    producto = {
        "name": {"es": "Boca titular"},
        "canonical_url": "https://tienda.example.com/productos/boca/",
        "variants": [{"price": "75000.00", "stock": 2, "values": [{"es": "L"}]}],
        "images": [
            {"position": 2, "src": "https://acdn-us.mitiendanube.com/x/segunda.png"},
            {"position": 1, "src": "https://acdn-us.mitiendanube.com/x/primera.jpg"},
        ],
    }

    texto = herramientas._describir_producto(producto)

    assert "[foto: https://acdn-us.mitiendanube.com/x/primera.jpg]" in texto


def test_una_foto_webp_no_se_ofrece():
    """WhatsApp no la muestra como imagen, así que mejor no mandarla."""
    producto = {
        "name": {"es": "Boca"},
        "variants": [{"price": "1", "stock": 1, "values": [{"es": "L"}]}],
        "images": [{"position": 1, "src": "https://acdn-us.mitiendanube.com/x/foto.webp"}],
    }

    assert "[foto:" not in herramientas._describir_producto(producto)


# -- Stock del mayorista ------------------------------------------------------
#
# El stock que se ofrece es el propio del mayorista (tabla mayorista_stock), no el
# de la tienda: Tiendanube aporta los nombres, el precio y el link.


def camiseta(id, nombre, stock_tienda=9, **extra):
    return {
        "id": id,
        "name": {"es": nombre},
        "canonical_url": f"https://tienda.example.com/productos/{id}/",
        "has_stock": stock_tienda > 0,
        "variants": [{"price": "75000.00", "stock": stock_tienda, "values": [{"es": "L"}]}],
        **extra,
    }


def test_con_stock_del_mayorista_los_talles_y_las_unidades_salen_de_ahi():
    p = camiseta(1, "Barcelona 26/27", stock_tienda=0)  # la tienda dice que no tiene nada

    texto = herramientas._describir_producto(p, {1: {"XL": 8, "M": 2, "S": 0, "L": 7}})

    assert "Talles con stock: M (2 u.), L (7 u.), XL (8 u.)" in texto, "en orden de talle y sin los talles en cero"
    assert "Sin stock" not in texto


def test_el_resultado_dice_las_categorias_para_saber_a_que_linea_mayorista_pertenece():
    p = camiseta(1, "Milan tercera 26/27", categories=[{"name": {"es": "G5"}}, {"name": {"es": "MILAN"}}])

    texto = herramientas._describir_producto(p, {1: {"M": 3}})

    assert "Categorías: G5, MILAN" in texto


def test_sin_categorias_el_resultado_no_trae_esa_linea():
    assert "Categorías" not in herramientas._describir_producto(camiseta(1, "Boca"), {1: {"M": 3}})


def test_un_modelo_de_la_tienda_que_no_esta_en_el_mayorista_no_se_ofrece():
    texto = herramientas._describir_producto(camiseta(2, "River Plate"), {1: {"M": 3}})

    assert "No figura en el catálogo mayorista" in texto
    assert "Talles con stock" not in texto, "aunque la tienda tenga stock, el mayorista no lo ofrece"


def test_un_modelo_del_mayorista_en_cero_dice_que_no_hay(monkeypatch):
    monkeypatch.setattr(herramientas, "_buscar_alternativas", lambda p, maximo=3, stock=None: [])

    texto = herramientas._describir_producto(camiseta(1, "Barcelona"), {1: {"M": 0}})

    assert "Sin stock por ahora." in texto


def test_las_alternativas_tambien_salen_del_stock_del_mayorista(monkeypatch):
    barcelona = camiseta(1, "Barcelona 26/27 Titular", stock_tienda=5)
    madrid = camiseta(2, "Barcelona 25/26 Titular", stock_tienda=0)  # la tienda no tiene, el mayorista sí
    otros = [camiseta(3, "Boca Juniors Retro", stock_tienda=5), camiseta(4, "Argentina Mundial", stock_tienda=5), camiseta(5, "River Plate Suplente", stock_tienda=5)]
    monkeypatch.setattr(herramientas, "_catalogo_completo", lambda: [barcelona, madrid, *otros])

    alternativas = herramientas._buscar_alternativas(barcelona, stock={1: {"M": 0}, 2: {"L": 4}})

    assert alternativas and "Barcelona 25/26" in alternativas[0]


def test_sin_base_se_usa_el_stock_de_la_tienda_como_antes(monkeypatch):
    monkeypatch.delenv("POSTGRES_DSN", raising=False)

    assert herramientas._stock_mayorista() is None
    assert "Talles con stock: L" in herramientas._describir_producto(camiseta(1, "Boca", stock_tienda=3))


def test_buscar_producto_usa_el_stock_del_mayorista(monkeypatch):
    monkeypatch.setenv("TIENDANUBE_STORE_ID", "1")
    monkeypatch.setenv("TIENDANUBE_ACCESS_TOKEN", "x")
    monkeypatch.setattr(herramientas, "_catalogo_completo", lambda: [camiseta(1, "Barcelona 26/27", stock_tienda=0)])
    monkeypatch.setattr(herramientas, "_stock_mayorista", lambda: {1: {"L": 7}})

    texto = herramientas.buscar_producto.invoke({"consulta": "barcelona"})

    assert "Barcelona 26/27" in texto and "L (7 u.)" in texto


def test_si_la_base_del_mayorista_falla_se_avisa_en_vez_de_inventar_stock(monkeypatch):
    monkeypatch.setenv("TIENDANUBE_STORE_ID", "1")
    monkeypatch.setenv("TIENDANUBE_ACCESS_TOKEN", "x")

    def cae():
        raise OSError("sin base")

    monkeypatch.setattr(herramientas, "_stock_mayorista", cae)

    texto = herramientas.buscar_producto.invoke({"consulta": "barcelona"})

    assert "No se pudo consultar" in texto and "sin base" in texto


@pytest.mark.parametrize(
    "consulta",
    ["camiseta de boca", "Holaaa capo, tenes alguna camiseta de boca en stock?", "boca"],
)
def test_pedir_una_camiseta_de_un_equipo_trae_los_modelos_de_ese_equipo_con_stock(monkeypatch, consulta):
    """Caso real: con 'camiseta de boca' entraban solo las que llevan 'Camiseta' en el nombre
    y el agente decía que de Boca no había nada, habiendo cuatro modelos con stock."""
    monkeypatch.setenv("TIENDANUBE_STORE_ID", "1")
    monkeypatch.setenv("TIENDANUBE_ACCESS_TOKEN", "x")
    catalogo = [
        camiseta(1, "BOCA JUNIORS VERSIÓN JUGADOR 26/27", tags="camiseta"),  # agotada en el mayorista
        camiseta(2, "Boca Alternativa 26/27"),
        camiseta(3, "Boca 26/27"),
        camiseta(4, "Boca 2015"),
        camiseta(5, "Camiseta PSG"),
        camiseta(6, "CAMISETA INTER DE MILÁN"),
        camiseta(7, "Camiseta LIVERPOOL 2025/26"),
        camiseta(8, "Argentina 1994 - Camiseta Retro"),
        camiseta(9, "River ORIGINAL"),
        camiseta(10, "Barcelona 26/27"),
    ]
    monkeypatch.setattr(herramientas, "_catalogo_completo", lambda: catalogo)
    monkeypatch.setattr(herramientas, "_stock_mayorista", lambda: {1: {"M": 0}, 2: {"M": 9}, 3: {"XL": 3}, 4: {"L": 1}, 5: {"M": 4}})

    texto = herramientas.buscar_producto.invoke({"consulta": consulta})

    assert "Boca Alternativa 26/27" in texto and "M (9 u.)" in texto
    assert "Boca 26/27" in texto and "Boca 2015" in texto
    assert "PSG" not in texto and "INTER" not in texto


def test_con_solo_camiseta_la_busqueda_sigue_funcionando(monkeypatch):
    assert herramientas._palabras_de_la_consulta("camiseta") == ["camiseta"]
    assert herramientas._palabras_de_la_consulta("¡Hola! ¿Tenés camisetas de River?") == ["river"]


def test_el_catalogo_se_trae_completo_aunque_pase_de_una_pagina(monkeypatch):
    paginas = {1: [{"id": i} for i in range(200)], 2: [{"id": 200 + i} for i in range(30)]}
    monkeypatch.setattr(herramientas, "_tiendanube_pedir", lambda ruta, parametros: paginas.get(parametros["page"], []))

    assert len(herramientas._catalogo_completo()) == 230


def test_en_produccion_sin_base_no_se_cae_al_stock_de_la_tienda(monkeypatch):
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.setenv("MODO", "produccion")

    with pytest.raises(RuntimeError, match="POSTGRES_DSN"):
        herramientas._stock_mayorista()
