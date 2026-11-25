"""Los modelos que ya vienen con dorsal o parche se cobran como «con parche y número».

Pasó con un pedido real: 9 camisetas con dorsal (Messi, Neymar, Haaland...) se
cobraron al precio de la lisa porque el cliente no tocó «con parche y número».
Ahora se decide talle por talle: un short puede tener «L, 17» (con número) y
«M, Sin número» (liso) en el mismo modelo.
"""

from __future__ import annotations

import pytest

from test_mayorista import LINEA_G5, LINEA_TOP, LINEA_TOP_PERS, guardar_web, pedido_web, producto, servidor  # noqa: F401
from test_mayorista import cache_limpia, cliente, negocio_de_ejemplo, propio  # noqa: F401  (fixtures)

import precios  # noqa: E402


@pytest.mark.parametrize("nombre, talle, esperado", [
    ("Barcelona 2015 Retro – Con Dorsal", "M, Messi 10", True),
    ("Liverpool Retro 2007 – Gerrard 8", "XL, Gerrard 8", True),
    ("PSG titular 26/27", "L, 7", True),
    ("Como 1907 Nico Paz – Parche Serie A", "XL", True),  # el nombre lo dice
    ("Short Bayern 26/27", "M, Sin número", False),
    ("Short Bayern 26/27", "L, 17", True),
    ("Manchester United Alternativa 1997/98 - Retro", "XXL", False),
    ("Boca Titular", "M", False),
])
def test_que_talle_ya_viene_con_dorsal(nombre, talle, esperado):
    assert precios.talle_personalizado(nombre, talle) is esperado


def test_un_modelo_con_dos_calidades_se_detecta():
    lineas = [LINEA_TOP, LINEA_TOP_PERS, LINEA_G5]

    assert [l["id"] for l in precios.lineas_posibles(["g5", "retro"], lineas)] == ["top", "g5"]
    assert [l["id"] for l in precios.lineas_posibles(["jugador"], lineas)] == ["top"], "la variante no cuenta"


@pytest.fixture
def tienda_con_dorsales(monkeypatch, propio):
    """1: Top con dorsal en todos sus talles. 2: short con y sin número. 3: Top liso."""
    cats = {1: ["jugador"], 2: ["shorts"], 3: ["retro"]}
    monkeypatch.setattr(servidor, "categorias_de", lambda p: cats[p["id"]])
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [
        producto(id=1, nombre="Barcelona 2015 Retro – Con Dorsal", talles=(("M, Messi 10", 20), ("L, Neymar 11", 20))),
        producto(id=2, nombre="Short Bayern 26/27", talles=(("L, 17", 20), ("M, Sin número", 20))),
        producto(id=3, nombre="Liverpool Alternativa 1995/96 - Retro", talles=(("M", 20), ("L", 20))),
    ])
    propio.cargar([(1, "M, Messi 10", 20), (1, "L, Neymar 11", 20), (2, "L, 17", 20), (2, "M, Sin número", 20), (3, "M", 20), (3, "L", 20)])
    shorts = {"id": "shorts", "nombre": "Shorts", "cats": ["shorts"], "minimo": 6, "tramos": [{"desde": 6, "precio": 35000}]}
    shorts_num = {"id": "shorts_num", "nombre": "Shorts con número", "cats": [], "personalizadaDe": "shorts", "minimo": 6, "tramos": [{"desde": 6, "precio": 38000}]}
    guardar_web(propio, lineas=[LINEA_TOP, LINEA_TOP_PERS, LINEA_G5, shorts, shorts_num], tramos=[])
    return propio


def test_la_web_marca_los_talles_con_dorsal(cliente, tienda_con_dorsales):
    productos = {p["id"]: p for p in cliente.get("/api/catalogo").json()["productos"]}

    assert productos[1]["personalizada"] is True and all(t.get("pers") for t in productos[1]["talles"])
    assert {t["talle"]: bool(t.get("pers")) for t in productos[2]["talles"]} == {"L, 17": True, "M, Sin número": False}
    assert productos[3]["personalizada"] is False


def test_el_servidor_cobra_el_dorsal_aunque_la_web_no_lo_marque(cliente, tienda_con_dorsales):
    cliente.get("/api/catalogo")
    items = [{"producto": 1, "talle": "M, Messi 10", "cantidad": 3}, {"producto": 1, "talle": "L, Neymar 11", "cantidad": 3}]

    cliente.post("/api/pedido", json=pedido_web(items=items))  # sin "pers": como el pedido de Alexis

    p = tienda_con_dorsales.pedido("A-7K3QX")
    assert all(i.get("pers") for i in p["datos"]["items"])
    assert p["total"] == 6 * 45000, "6 con dorsal pagan el precio de «con parche y número», no el de la lisa"


def test_en_un_short_solo_los_talles_con_numero_van_con_numero(cliente, tienda_con_dorsales):
    cliente.get("/api/catalogo")
    items = [{"producto": 2, "talle": "L, 17", "cantidad": 6}, {"producto": 2, "talle": "M, Sin número", "cantidad": 6}]

    cliente.post("/api/pedido", json=pedido_web(items=items))

    p = tienda_con_dorsales.pedido("A-7K3QX")
    assert [bool(i.get("pers")) for i in p["datos"]["items"]] == [True, False]
    assert p["total"] == 6 * 38000 + 6 * 35000


# -- La categoría «CON DORSAL» y la calidad «ORIGINAL» de Tiendanube ----------------------


@pytest.fixture
def tienda_por_categoria(monkeypatch, propio):
    """Como quedó la tienda: 1 solo en «CON DORSAL», 2 solo en «ORIGINAL» (lisa), 3 en G5."""
    filtros = {1: [], 2: [], 3: ["g5"]}  # «CON DORSAL» y «ORIGINAL» no son de ninguna línea por los filtros
    monkeypatch.setattr(servidor, "categorias_de", lambda p: filtros[p["id"]])
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [
        producto(id=1, nombre="Real Madrid Retro 17/18 – Cristiano Ronaldo 7", categorias=("CON DORSAL",), talles=(("M", 20), ("L", 20))),
        producto(id=2, nombre="Marsella suplente versión jugador", categorias=("ORIGINAL",), talles=(("M", 20), ("L", 20), ("XL", 20))),
        producto(id=3, nombre="Boca Titular", categorias=("G5",), talles=(("M", 20), ("L", 20))),
    ])
    propio.cargar([(1, "M", 20), (1, "L", 20), (2, "M", 20), (2, "L", 20), (2, "XL", 20), (3, "M", 20), (3, "L", 20)])
    guardar_web(propio, lineas=[LINEA_TOP, LINEA_TOP_PERS, LINEA_G5], tramos=[], categoriaDorsal="CON DORSAL",
                sinonimosCalidad={"ORIGINAL": "top", "CON DORSAL": "top"})
    return propio


def test_con_dorsal_y_original_entran_en_top_quality(cliente, tienda_por_categoria):
    productos = {p["id"]: p for p in cliente.get("/api/catalogo").json()["productos"]}

    assert productos[1]["linea"] == "top" and all(t.get("pers") for t in productos[1]["talles"]), "con dorsal: todos sus talles"
    assert productos[2]["linea"] == "top" and not productos[2]["personalizada"], "original sin dorsal: lisa"
    assert productos[3]["linea"] == "g5"


def test_un_pedido_con_dorsal_se_cobra_como_con_parche(cliente, tienda_por_categoria):
    cliente.get("/api/catalogo")
    items = [{"producto": 1, "talle": "M", "cantidad": 3}, {"producto": 1, "talle": "L", "cantidad": 3},
             {"producto": 2, "talle": "M", "cantidad": 2}, {"producto": 2, "talle": "L", "cantidad": 2}, {"producto": 2, "talle": "XL", "cantidad": 2}]

    cliente.post("/api/pedido", json=pedido_web(items=items))

    assert tienda_por_categoria.pedido("A-7K3QX")["total"] == 6 * 45000 + 6 * 35000


def test_una_calidad_que_apunta_a_una_linea_inexistente_no_se_guarda():
    import ajustes

    a = ajustes.AjustesWeb(**{**ajustes.por_defecto({}, "Tienda", "5491100000000", "", True, 6, []), "sinonimosCalidad": {"ORIGINAL": "nada"}})

    assert any("no existe" in m for m in ajustes.problemas(a))
