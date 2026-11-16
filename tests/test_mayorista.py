"""Pruebas de la web mayorista. No salen a internet.

Tiendanube se reemplaza por productos escritos a mano: lo que se prueba es lo
nuestro (qué entra al catálogo, cómo se ordenan los talles, qué pasa cuando
Tiendanube no contesta), no que la API de ellos ande.

    pytest
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mayorista"))

import almacen  # noqa: E402
import sitio as servidor  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

FOTO = "https://acdn-us.mitiendanube.com/stores/1/products/boca-abc-1024-1024.png"


def producto(
    id=1,
    nombre="Boca Juniors - Titular 26/27 - Versión Jugador",
    publicado=True,
    talles=(("M", 2), ("S", 1), ("XL", 3)),
    categorias=("BOCA JUNIORS", "FÚTBOL ARGENTINO"),
    fotos=1,
) -> dict:
    return {
        "id": id,
        "name": {"es": nombre},
        "published": publicado,
        "categories": [{"name": {"es": c}} for c in categorias],
        "variants": [{"stock": s, "values": [{"es": t}]} for t, s in talles],
        "images": [{"position": i + 1, "src": FOTO.replace("abc", f"abc{i}")} for i in range(fotos)],
    }


@pytest.fixture(autouse=True)
def cache_limpia():
    servidor._cache.update(t=0.0, productos=None, imagenes={})
    servidor.tn._fotos.clear()
    servidor._intentos.clear()
    servidor._categorias_cache.update(t=0.0, valor=None)
    yield


def _tramos(negocio: dict) -> list[dict]:
    return sorted(
        ({"desde": int(t["desde"]), "precio": int(t["precio"])} for t in negocio.get("tramos") or []),
        key=lambda t: t["desde"],
        reverse=True,
    )


@pytest.fixture(autouse=True)
def negocio_de_ejemplo(monkeypatch):
    """Los tests no dependen de negocio.json (es de cada cliente): rigen los datos de ejemplo."""
    negocio = json.loads((Path(servidor.__file__).parent / "negocio.ejemplo.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(servidor, "NEGOCIO", negocio)
    monkeypatch.setattr(servidor, "MARCA", negocio["marca"])
    monkeypatch.setattr(servidor, "MINIMO", int(negocio["minimo"]))
    monkeypatch.setattr(servidor, "CATEGORIAS", negocio["categorias"])
    monkeypatch.setattr(servidor, "TRAMOS", _tramos(negocio))


@pytest.fixture
def cliente():
    return TestClient(servidor.app)


@pytest.fixture
def propio(monkeypatch):
    """El stock propio del mayorista (en producción es Postgres)."""
    a = almacen.AlmacenMemoria()
    monkeypatch.setattr(servidor, "_almacen", a)
    return a


# -- Qué entra al catálogo (sin base propia: el stock de la tienda, solo en desarrollo) ------


def test_entran_los_publicados_con_stock():
    catalogo, _ = servidor.armar_catalogo([producto()])

    assert [p["id"] for p in catalogo] == [1]


def test_no_entran_los_ocultos():
    """Si en la tienda no se ve, en el mayorista tampoco."""
    catalogo, _ = servidor.armar_catalogo([producto(publicado=False)])

    assert catalogo == []


def test_no_entra_lo_que_no_tiene_stock():
    catalogo, _ = servidor.armar_catalogo([producto(talles=(("M", 0), ("L", 0)))])

    assert catalogo == []


def test_un_talle_sin_stock_no_se_ofrece():
    catalogo, _ = servidor.armar_catalogo([producto(talles=(("M", 0), ("L", 2)))])

    assert [t["talle"] for t in catalogo[0]["talles"]] == ["L"]


def test_sin_control_de_stock_no_se_ofrece():
    """stock None = Tiendanube no lo cuenta: en mayorista hace falta saber cuántas hay."""
    p = producto()
    p["variants"][0]["stock"] = None

    catalogo, _ = servidor.armar_catalogo([p])

    assert "M" not in [t["talle"] for t in catalogo[0]["talles"]]


def test_sin_foto_no_entra():
    catalogo, _ = servidor.armar_catalogo([producto(fotos=0)])

    assert catalogo == []


def test_los_talles_salen_en_orden_de_talle_y_no_alfabetico():
    catalogo, _ = servidor.armar_catalogo([producto(talles=(("XL", 1), ("S", 1), ("L", 1), ("M", 1)))])

    assert [t["talle"] for t in catalogo[0]["talles"]] == ["S", "M", "L", "XL"]


def test_el_stock_es_el_de_tiendanube():
    catalogo, _ = servidor.armar_catalogo([producto(talles=(("M", 7),))])

    assert catalogo[0]["talles"] == [{"talle": "M", "stock": 7}]


def test_las_fotos_se_piden_chicas():
    _, imagenes = servidor.armar_catalogo([producto(fotos=2)])

    assert imagenes[1][0].endswith("-480-0.webp")
    assert len(imagenes[1]) == 2


# -- Nombre, versión y categorías ---------------------------------------------


@pytest.mark.parametrize(
    "crudo, esperado",
    [
        ("Boca Juniors - Titular 26/27 - Versión Jugador", "Boca Juniors - Titular 26/27"),
        ("Camiseta Argentina 2026 alternativa (Versión Jugador)", "Argentina 2026 alternativa"),
        ("Argentina Titular 2026 | Tela jugador - Escudos termosellados", "Argentina Titular 2026"),
        ("Manchester City - Alternativa 25/26 - Versión Jugador FODEN #47", "Manchester City - Alternativa 25/26"),
        ("River Plate - Titular 26/27 - Otamendi #30", "River Plate - Titular 26/27 - Otamendi #30"),
    ],
)
def test_el_nombre_se_limpia(crudo, esperado):
    assert servidor.nombre_limpio(crudo) == esperado


def test_la_version_jugador_se_detecta_por_el_nombre():
    assert servidor.version_de("Boca - Versión Jugador") == "jugador"
    assert servidor.version_de("Argentina Titular | Tela jugador") == "jugador"
    assert servidor.version_de("River Plate - Titular 26/27") == "fan"


def test_las_categorias_de_la_tienda_se_traducen():
    assert servidor.categorias_de(producto(categorias=("BOCA JUNIORS", "FÚTBOL ARGENTINO"))) == ["arg"]
    assert servidor.categorias_de(producto(categorias=("SELECCIÓN ARGENTINA", "RETRO / CLÁSICAS"))) == ["sel", "retro"]
    assert servidor.categorias_de(producto(categorias=("FÚTBOL INTERNACIONAL",))) == ["int"]


# -- La API -------------------------------------------------------------------


def test_la_api_devuelve_catalogo_y_reglas(cliente, monkeypatch):
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto()])

    r = cliente.get("/api/catalogo")

    assert r.status_code == 200
    d = r.json()
    assert d["minimo"] == 6
    assert d["tramos"] == [{"desde": 12, "precio": 25000}, {"desde": 6, "precio": 28000}]
    assert d["productos"][0]["nombre"] == "Boca Juniors - Titular 26/27"
    assert d["productos"][0]["version"] == "jugador"
    assert "no-store" in r.headers["cache-control"]


def test_el_catalogo_se_guarda_unos_minutos(cliente, monkeypatch):
    llamadas = []
    monkeypatch.setattr(servidor, "_traer_productos", lambda: llamadas.append(1) or [producto()])

    cliente.get("/api/catalogo")
    cliente.get("/api/catalogo")

    assert len(llamadas) == 1


def test_si_tiendanube_se_cae_se_muestra_el_ultimo_catalogo(cliente, monkeypatch):
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto()])
    cliente.get("/api/catalogo")
    servidor._cache["t"] = time.time() - servidor.CACHE_SEGUNDOS - 1  # venció

    def cae():
        raise OSError("sin conexión")

    monkeypatch.setattr(servidor, "_traer_productos", cae)
    r = cliente.get("/api/catalogo")

    assert r.status_code == 200
    assert len(r.json()["productos"]) == 1


def test_si_tiendanube_se_cae_y_no_hay_nada_avisa(cliente, monkeypatch):
    def cae():
        raise OSError("sin conexión")

    monkeypatch.setattr(servidor, "_traer_productos", cae)

    assert cliente.get("/api/catalogo").status_code == 503


def test_una_foto_que_no_existe_da_404(cliente, monkeypatch):
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto()])

    assert cliente.get("/img/999/1").status_code == 404
    assert cliente.get("/img/1/9").status_code == 404


def test_la_pagina_y_la_salud_responden(cliente):
    assert cliente.get("/salud").json() == {"estado": "ok"}
    r = cliente.get("/")
    assert r.status_code == 200
    assert "Mayorista" in r.text


def test_la_api_trae_las_condiciones_de_compra(cliente, monkeypatch):
    """Lo pidió el negocio: las condiciones tienen que llegar a la web (salen de negocio.json)."""
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto()])

    d = cliente.get("/api/catalogo").json()

    assert d["condiciones"].startswith("Condiciones:")
    assert len(d["reglas"]) == 3
    assert {r["etiqueta"] for r in d["reglas"]} == {"Antes de pedir", "Envío", "Pago"}
    assert [c["id"] for c in d["categorias"]] == ["arg", "sel", "int", "retro"]


def test_el_favicon_y_los_iconos_se_sirven(cliente):
    """Un favicon que da 404 deja la pestaña con el ícono genérico del navegador."""
    for ruta, tipo in [
        ("/favicon.ico", "icon"),
        ("/logo.webp", "webp"),
        ("/apple-touch-icon.png", "png"),
        ("/icon-192.png", "png"),
        ("/icon-512.png", "png"),
        ("/site.webmanifest", None),
    ]:
        r = cliente.get(ruta)
        assert r.status_code == 200, ruta
        if tipo:
            assert tipo in r.headers["content-type"], ruta


def test_la_pagina_enlaza_el_favicon(cliente):
    texto = cliente.get("/").text

    assert 'rel="icon" href="favicon.ico"' in texto
    assert 'rel="apple-touch-icon"' in texto
    assert 'rel="manifest"' in texto


def test_el_filtro_de_version_se_puede_apagar(cliente, monkeypatch):
    """Con "filtroVersion": false en negocio.json la web usa solo las categorías de la tienda."""
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto()])

    assert cliente.get("/api/catalogo").json()["filtroVersion"] is True

    monkeypatch.setattr(servidor, "NEGOCIO", {**servidor.NEGOCIO, "filtroVersion": False})
    servidor._cache["t"] = 0.0

    assert cliente.get("/api/catalogo").json()["filtroVersion"] is False


# -- Con stock propio ------------------------------------------------------------


def test_con_stock_propio_el_catalogo_usa_ese_stock_y_no_el_de_la_tienda(cliente, monkeypatch, propio):
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto()])  # la tienda dice M2 S1 XL3
    propio.cargar([(1, "S", 0), (1, "M", 9), (1, "L", 4)])

    talles = cliente.get("/api/catalogo").json()["productos"][0]["talles"]

    assert [(t["talle"], t["stock"]) for t in talles] == [("M", 9), ("L", 4)]


def test_un_modelo_de_la_tienda_que_no_esta_en_el_mayorista_no_se_ofrece(cliente, monkeypatch, propio):
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto(id=1), producto(id=2, nombre="River Plate - Titular")])
    propio.cargar([(1, "M", 3)])

    ids = [p["id"] for p in cliente.get("/api/catalogo").json()["productos"]]

    assert ids == [1]


def test_un_modelo_del_mayorista_sin_stock_no_se_ofrece(cliente, monkeypatch, propio):
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto()])
    propio.cargar([(1, "M", 0)])

    assert cliente.get("/api/catalogo").json()["productos"] == []


def test_un_modelo_oculto_en_la_tienda_no_se_ofrece_aunque_tenga_stock_propio(cliente, monkeypatch, propio):
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto(publicado=False)])
    propio.cargar([(1, "M", 3)])

    assert cliente.get("/api/catalogo").json()["productos"] == []


def test_el_stock_se_actualiza_al_instante_sin_esperar_la_cache_de_la_tienda(cliente, monkeypatch, propio):
    llamadas = []
    monkeypatch.setattr(servidor, "_traer_productos", lambda: llamadas.append(1) or [producto()])
    propio.cargar([(1, "M", 5)])
    cliente.get("/api/catalogo")
    fila = propio.filas()[0]

    propio.aplicar([(fila["id"], 5, 2)], [])
    talles = cliente.get("/api/catalogo").json()["productos"][0]["talles"]

    assert talles == [{"talle": "M", "stock": 2}]
    assert len(llamadas) == 1, "la tienda se leyó una sola vez: solo el stock es fresco"


def test_si_la_base_falla_el_catalogo_avisa_en_vez_de_mostrar_stock_viejo(cliente, monkeypatch, propio):
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto()])

    def cae(*a, **k):
        raise OSError("sin base")

    monkeypatch.setattr(propio, "filas", cae)

    assert cliente.get("/api/catalogo").status_code == 503


# -- Pedidos desde la web ---------------------------------------------------------


def pedido_web(**extra):
    return {"codigo": "A-7K3QX", "items": [{"producto": 1, "talle": "M", "cantidad": 2}], "nombre": "Pablo", "negocio": "Kiosco", "notas": "", **extra}


def test_un_pedido_se_guarda_con_su_codigo_y_no_descuenta_stock(cliente, monkeypatch, propio):
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto()])
    propio.cargar([(1, "M", 5)])
    cliente.get("/api/catalogo")  # trae los nombres

    r = cliente.post("/api/pedido", json=pedido_web(items=[{"producto": 1, "talle": "M", "cantidad": 2}, {"producto": 1, "talle": "S", "cantidad": 4}]))

    assert r.status_code == 200 and r.json() == {"ok": True, "codigo": "A-7K3QX", "nuevo": True}
    p = propio.pedido("A-7K3QX")
    assert (p["estado"], p["unidades"]) == ("nuevo", 6)
    assert p["total"] == 6 * 28000, "6 camisetas entran en el primer tramo"
    assert p["datos"]["items"][0]["nombre"] == "Boca Juniors - Titular 26/27"
    assert (p["datos"]["nombre"], p["datos"]["negocio"]) == ("Pablo", "Kiosco")
    assert propio.filas()[0]["cantidad"] == 5, "el stock se descuenta recién cuando el equipo confirma"


def test_mandar_dos_veces_el_mismo_pedido_no_lo_duplica(cliente, propio):
    cliente.post("/api/pedido", json=pedido_web())

    r = cliente.post("/api/pedido", json=pedido_web())

    assert r.status_code == 200 and r.json()["nuevo"] is False
    assert len(propio.pedidos()) == 1


@pytest.mark.parametrize("codigo", ["", "a-7k3qx", "A-7K3Q", "A-7K3QXX", "B-7K3QX", "A-7K 3Q"])
def test_un_codigo_con_otro_formato_no_se_acepta(cliente, propio, codigo):
    assert cliente.post("/api/pedido", json=pedido_web(codigo=codigo)).status_code == 422


@pytest.mark.parametrize("items", [[], [{"producto": 1, "talle": "M", "cantidad": 0}], [{"producto": 1, "talle": "", "cantidad": 1}], [{"producto": 1, "talle": "M", "cantidad": 1000}]])
def test_un_pedido_vacio_o_con_cantidades_raras_no_se_acepta(cliente, propio, items):
    assert cliente.post("/api/pedido", json=pedido_web(items=items)).status_code == 422


def test_hay_un_tope_de_pedidos_por_hora_desde_la_misma_direccion(cliente, propio, monkeypatch):
    monkeypatch.setattr(servidor, "PEDIDOS_POR_HORA", 2)

    estados = [cliente.post("/api/pedido", json=pedido_web(codigo=f"A-0000{i}")).status_code for i in range(3)]

    assert estados == [200, 200, 429]


def test_sin_base_los_pedidos_responden_503_y_la_web_igual_manda_el_whatsapp(cliente, monkeypatch):
    monkeypatch.setattr(servidor, "_almacen", None)

    assert cliente.post("/api/pedido", json=pedido_web()).status_code == 503


def test_el_precio_total_estimado_usa_el_tramo_que_corresponde(cliente, propio):
    cliente.post("/api/pedido", json=pedido_web(items=[{"producto": 1, "talle": "M", "cantidad": 12}]))

    assert propio.pedido("A-7K3QX")["total"] == 12 * 25000


def test_un_pedido_de_un_modelo_desconocido_se_guarda_con_un_nombre_generico(cliente, propio):
    cliente.post("/api/pedido", json=pedido_web(items=[{"producto": 77, "talle": "M", "cantidad": 1}]))

    assert propio.pedido("A-7K3QX")["datos"]["items"][0]["nombre"] == "Modelo 77"


# -- Filtros que se editan en el panel ----------------------------------------------


def test_sin_filtros_guardados_rigen_los_de_negocio_json(cliente, monkeypatch, propio):
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto()])
    propio.cargar([(1, "M", 3)])

    d = cliente.get("/api/catalogo").json()

    assert [c["id"] for c in d["categorias"]] == ["arg", "sel", "int", "retro"]
    assert d["productos"][0]["cats"] == ["arg"]


def test_los_filtros_del_panel_reemplazan_a_los_de_negocio_json(cliente, monkeypatch, propio):
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto()])
    propio.cargar([(1, "M", 3)])
    propio.guardar_config("categorias", [{"id": "boca", "nombre": "Boca", "tiendanube": ["BOCA JUNIORS"]}])

    d = cliente.get("/api/catalogo").json()

    assert d["categorias"] == [{"id": "boca", "nombre": "Boca"}]
    assert d["productos"][0]["cats"] == ["boca"]


def test_sin_filtros_la_web_no_ofrece_ninguno(cliente, monkeypatch, propio):
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto()])
    propio.cargar([(1, "M", 3)])
    propio.guardar_config("categorias", [])

    d = cliente.get("/api/catalogo").json()

    assert d["categorias"] == [] and d["productos"][0]["cats"] == []


def test_un_filtro_cambiado_se_ve_al_invalidar_la_cache(cliente, monkeypatch, propio):
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto()])
    propio.cargar([(1, "M", 3)])
    cliente.get("/api/catalogo")
    propio.guardar_config("categorias", [{"id": "x", "nombre": "Equis", "tiendanube": []}])

    servidor._invalidar_cache()

    assert [c["id"] for c in cliente.get("/api/catalogo").json()["categorias"]] == ["x"]


def test_si_la_base_falla_los_filtros_siguen_con_los_ultimos_conocidos(cliente, monkeypatch, propio):
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto()])
    propio.cargar([(1, "M", 3)])
    propio.guardar_config("categorias", [{"id": "x", "nombre": "Equis", "tiendanube": []}])
    cliente.get("/api/catalogo")
    servidor._categorias_cache["t"] = 0.0  # venció

    def cae(clave):
        raise OSError("sin base")

    monkeypatch.setattr(propio, "config", cae)

    assert [c["id"] for c in cliente.get("/api/catalogo").json()["categorias"]] == ["x"]
