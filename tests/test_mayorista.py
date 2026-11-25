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
    servidor._web_cache.update(t=0.0, valor=None)
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


def test_www_redirige_al_dominio_sin_www(cliente):
    r = cliente.get("/admin/?x=1", headers={"host": "www.mayoristasf.digital"}, follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "https://mayoristasf.digital/admin/?x=1"
    # sin www no redirige, y la salud responde siempre (para el chequeo del servidor)
    assert cliente.get("/salud", headers={"host": "www.mayoristasf.digital"}).status_code == 200
    assert cliente.get("/", headers={"host": "mayoristasf.digital"}, follow_redirects=False).status_code == 200


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


# -- Los ajustes que se editan en el panel -----------------------------------------


def guardar_web(propio, **cambios):
    propio.guardar_config("web", {**servidor._ajustes_de_siempre(), **cambios})
    servidor._web_cache.update(t=0.0, valor=None)


def test_sin_ajustes_guardados_la_web_usa_negocio_json(cliente, monkeypatch, propio):
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto()])
    propio.cargar([(1, "M", 3)])

    d = cliente.get("/api/catalogo").json()

    assert (d["titulo"], d["bandaIzq"], d["pausa"], d["aviso"], d["color"]) == ("Camisetas por mayor.", "Solo mayoristas", False, "", "#1fd65f")
    assert d["mensaje"] == servidor.ajustes.MENSAJE_POR_DEFECTO
    assert d["logo"] == "logo.webp"


def test_los_ajustes_del_panel_llegan_a_la_web(cliente, monkeypatch, propio):
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto()])
    propio.cargar([(1, "M", 3)])
    guardar_web(propio, marca="Otra Marca", whatsapp="5493562514675", minimo=10, color="#3aa9ff", titulo="Todo por mayor.", aviso="Cerrado por balance",
                tramos=[{"desde": 10, "precio": 30000}, {"desde": 20, "precio": 27000}], horario="Lunes a viernes de 9 a 18", instagram="https://instagram.com/sf",
                mostrarUnidades=False, filtroVersion=False, logoHash="abc12345")

    d = cliente.get("/api/catalogo").json()

    assert (d["marca"], d["whatsapp"], d["minimo"], d["color"], d["titulo"], d["aviso"]) == ("Otra Marca", "5493562514675", 10, "#3aa9ff", "Todo por mayor.", "Cerrado por balance")
    assert d["tramos"] == [{"desde": 20, "precio": 27000}, {"desde": 10, "precio": 30000}], "de mayor a menor, como espera la web"
    assert (d["horario"], d["instagram"], d["mostrarUnidades"], d["filtroVersion"]) == ("Lunes a viernes de 9 a 18", "https://instagram.com/sf", False, False)
    assert d["logo"] == "logo.webp?v=abc12345"


def test_con_los_precios_ocultos_no_se_mandan(cliente, monkeypatch, propio):
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto()])
    propio.cargar([(1, "M", 3)])
    guardar_web(propio, ocultarPrecios=True)

    assert cliente.get("/api/catalogo").json()["tramos"] == []


def test_un_enlace_a_la_tienda_que_no_es_web_no_se_ofrece(cliente, monkeypatch, propio):
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto()])
    propio.cargar([(1, "M", 3)])
    guardar_web(propio, tiendaUrl="javascript:alert(1)")

    assert cliente.get("/api/catalogo").json()["tiendaUrl"] == ""


def test_los_ajustes_que_ya_no_existen_se_ignoran(cliente, monkeypatch, propio):
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto()])
    propio.cargar([(1, "M", 3)])
    propio.guardar_config("web", {"marca": "Otra", "campoViejo": "x"})
    servidor._web_cache.update(t=0.0, valor=None)

    d = cliente.get("/api/catalogo").json()

    assert d["marca"] == "Otra" and "campoViejo" not in d and d["minimo"] == 6


def test_el_orden_del_catalogo_se_elige(cliente, monkeypatch, propio):
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto(id=1, nombre="Boca"), producto(id=2, nombre="River"), producto(id=3, nombre="Argentina")])
    propio.cargar([(1, "M", 9), (2, "M", 1), (3, "M", 5)])

    def orden(valor):
        guardar_web(propio, orden=valor)
        return [p["id"] for p in cliente.get("/api/catalogo").json()["productos"]]

    assert orden("novedades") == [3, 2, 1]
    assert orden("stock") == [1, 3, 2]
    assert orden("nombre") == [3, 1, 2]


def test_con_los_pedidos_en_pausa_no_se_guardan_pedidos(cliente, monkeypatch, propio):
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto()])
    guardar_web(propio, pausa=True)

    r = cliente.post("/api/pedido", json=pedido_web())

    assert r.status_code == 409
    assert propio.pedidos() == []


def test_el_total_estimado_usa_los_precios_del_panel(cliente, monkeypatch, propio):
    guardar_web(propio, tramos=[{"desde": 3, "precio": 1000}])

    cliente.post("/api/pedido", json=pedido_web(items=[{"producto": 1, "talle": "M", "cantidad": 4}]))

    assert propio.pedido("A-7K3QX")["total"] == 4000


# -- Precios por línea (G5, Top Quality, Shorts) ----------------------------------------

LINEA_G5 = {"id": "g5", "nombre": "G5", "cats": ["g5"], "minimo": 6, "tramos": [{"desde": 6, "precio": 33500}, {"desde": 12, "precio": 26500}]}
LINEA_TOP = {"id": "top", "nombre": "Top Quality", "cats": ["jugador", "retro"], "minimo": 6, "tramos": [{"desde": 6, "precio": 35000}, {"desde": 12, "precio": 30000}]}


@pytest.fixture
def con_lineas(monkeypatch, propio):
    """Tres modelos: 1 es G5, 2 es Top Quality, 3 no tiene categoría. Y las dos líneas guardadas."""
    cats = {1: ["g5"], 2: ["jugador"], 3: []}
    monkeypatch.setattr(servidor, "categorias_de", lambda p: cats[p["id"]])
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto(id=1), producto(id=2), producto(id=3)])
    propio.cargar([(1, "M", 20), (2, "M", 20), (3, "M", 20)])
    guardar_web(propio, lineas=[LINEA_TOP, LINEA_G5], tramos=[])
    return propio


def test_la_web_recibe_las_lineas_con_las_escalas_de_mayor_a_menor(cliente, con_lineas):
    d = cliente.get("/api/catalogo").json()

    assert [l["id"] for l in d["lineas"]] == ["top", "g5"]
    assert d["lineas"][1]["tramos"] == [{"desde": 12, "precio": 26500}, {"desde": 6, "precio": 33500}]
    assert d["lineas"][1]["minimo"] == 6


def test_cada_modelo_sabe_a_que_linea_pertenece(cliente, con_lineas):
    productos = {p["id"]: p["linea"] for p in cliente.get("/api/catalogo").json()["productos"]}

    assert productos == {1: "g5", 2: "top", 3: None}


def test_con_los_precios_ocultos_las_lineas_van_sin_escalas_pero_con_su_minimo(cliente, con_lineas):
    guardar_web(con_lineas, lineas=[LINEA_TOP, LINEA_G5], ocultarPrecios=True)

    lineas = cliente.get("/api/catalogo").json()["lineas"]

    assert [l["tramos"] for l in lineas] == [[], []]
    assert [l["minimo"] for l in lineas] == [6, 6]


def test_sin_lineas_la_web_sigue_con_la_escala_unica(cliente, monkeypatch, propio):
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto()])
    propio.cargar([(1, "M", 3)])

    d = cliente.get("/api/catalogo").json()

    assert d["lineas"] == [] and d["productos"][0]["linea"] is None


def test_el_total_de_un_pedido_se_calcula_por_linea(cliente, con_lineas):
    cliente.get("/api/catalogo")  # trae los modelos
    items = [{"producto": 1, "talle": "M", "cantidad": 6}, {"producto": 2, "talle": "M", "cantidad": 6}]

    cliente.post("/api/pedido", json=pedido_web(items=items))

    assert con_lineas.pedido("A-7K3QX")["total"] == 6 * 33500 + 6 * 35000, "6 G5 y 6 Top Quality no se mezclan en la escala de 12"


def test_doce_de_la_misma_linea_pagan_la_escala_de_doce(cliente, con_lineas):
    cliente.get("/api/catalogo")

    cliente.post("/api/pedido", json=pedido_web(items=[{"producto": 1, "talle": "M", "cantidad": 12}]))

    assert con_lineas.pedido("A-7K3QX")["total"] == 12 * 26500


def test_un_pedido_con_una_linea_bajo_su_minimo_queda_sin_total(cliente, con_lineas):
    cliente.get("/api/catalogo")
    items = [{"producto": 1, "talle": "M", "cantidad": 6}, {"producto": 2, "talle": "M", "cantidad": 3}]

    cliente.post("/api/pedido", json=pedido_web(items=items))

    assert con_lineas.pedido("A-7K3QX")["total"] is None


def test_un_modelo_sin_linea_y_sin_escala_general_deja_el_total_a_confirmar(cliente, con_lineas):
    cliente.get("/api/catalogo")

    cliente.post("/api/pedido", json=pedido_web(items=[{"producto": 3, "talle": "M", "cantidad": 6}]))

    assert con_lineas.pedido("A-7K3QX")["total"] is None


def test_el_logo_subido_en_el_panel_reemplaza_al_de_la_carpeta(cliente, propio):
    estatico = cliente.get("/logo.webp")
    assert estatico.status_code == 200 and estatico.headers["content-type"].startswith("image/webp")

    png = b"\x89PNG\r\n\x1a\n" + b"0" * 20
    propio.guardar_config("logo", {"tipo": "image/png", "datos": __import__("base64").b64encode(png).decode(), "hash": "x"})
    propio_logo = cliente.get("/logo.webp")

    assert propio_logo.content == png and propio_logo.headers["content-type"] == "image/png"


def test_sin_logo_guardado_vuelve_al_de_la_carpeta(cliente, propio):
    propio.guardar_config("logo", {})

    assert cliente.get("/logo.webp").headers["content-type"].startswith("image/webp")


def test_el_titulo_y_la_descripcion_para_buscadores_van_en_la_pagina(cliente, propio):
    guardar_web(propio, seoTitulo='Mayorista <SF> & "Camisetas"', seoDescripcion="Camisetas por mayor, con stock real.")

    texto = cliente.get("/").text

    assert "<title>Mayorista &lt;SF&gt; &amp; &quot;Camisetas&quot;</title>" in texto or "<title>Mayorista &lt;SF&gt; &amp; \"Camisetas\"</title>" in texto
    assert 'content="Camisetas por mayor, con stock real."' in texto


def test_sin_titulo_para_buscadores_la_pagina_queda_como_estaba(cliente, propio):
    assert "<title>Mayorista</title>" in cliente.get("/").text


# -- El mensaje del pedido (JavaScript: se prueba con node, si está) ----------------

NODE = __import__("shutil").which("node")
PAGINAS = [servidor.CARPETA / "static" / "index.html", servidor.CARPETA / "static" / "admin" / "index.html"]


def funcion_renderizar(pagina) -> str:
    texto = pagina.read_text(encoding="utf-8")
    inicio = texto.index("function renderMensaje")
    fin = texto.index("\n}\n", inicio) + 3
    return texto[inicio:fin]


def renderizar(plantilla: str, datos: dict) -> str:
    import json
    import subprocess

    codigo = funcion_renderizar(PAGINAS[0]) + f"\nprocess.stdout.write(renderMensaje({json.dumps(plantilla)}, {json.dumps(datos)}));"
    return subprocess.run([NODE, "-e", codigo], capture_output=True, text=True, encoding="utf-8", check=True).stdout


DATOS = {"marca": "SF", "codigo": "A-7K3QX", "nombre": "Pablo", "negocio": "Kiosco", "pedido": "1) Boca\n   M x2", "unidades": "2 camisetas", "precio": "$28.000", "total": "$56.000", "notas": "", "condiciones": ""}


@pytest.mark.skipif(not NODE, reason="hace falta node")
def test_el_mensaje_por_defecto_sale_como_siempre_y_sin_lineas_vacias():
    texto = renderizar(servidor.ajustes.MENSAJE_POR_DEFECTO, DATOS)

    assert texto == "*PEDIDO MAYORISTA - SF*\nPedido: A-7K3QX\nNombre: Pablo\nNegocio / ciudad: Kiosco\n\n1) Boca\n   M x2\n\nTotal: 2 camisetas\nPrecio por unidad: $28.000\nTotal: $56.000"


@pytest.mark.skipif(not NODE, reason="hace falta node")
def test_las_lineas_con_datos_vacios_no_se_mandan():
    texto = renderizar(servidor.ajustes.MENSAJE_POR_DEFECTO, {**DATOS, "nombre": "", "negocio": "", "precio": "", "total": ""})

    assert "Nombre" not in texto and "Negocio" not in texto and "Precio por unidad" not in texto
    assert "Total: 2 camisetas" in texto
    assert "\n\n\n" not in texto


@pytest.mark.skipif(not NODE, reason="hace falta node")
def test_un_mensaje_propio_se_arma_con_los_datos():
    assert renderizar("Hola, soy {nombre} de {negocio}.\n{pedido}", DATOS) == "Hola, soy Pablo de Kiosco.\n1) Boca\n   M x2"


def test_la_web_y_el_panel_arman_el_mensaje_con_el_mismo_codigo():
    assert funcion_renderizar(PAGINAS[0]) == funcion_renderizar(PAGINAS[1])


# -- Avisos por mail desde la web -------------------------------------------------------------


class AgenteQueAnota:
    def __init__(self):
        self.avisos = []

    def avisar(self, asunto, cuerpo, tipo):
        self.avisos.append((asunto, cuerpo, tipo))
        return {"enviado": True}


@pytest.fixture
def con_avisos(monkeypatch, propio):
    agente = AgenteQueAnota()
    monkeypatch.setattr(servidor, "_avisador", servidor.avisos.Avisador(propio, agente, sincrono=True))
    return agente


def test_un_pedido_nuevo_avisa_por_mail(cliente, propio, con_avisos):
    cliente.post("/api/pedido", json=pedido_web(items=[{"producto": 1, "talle": "M", "cantidad": 6}]))

    asunto, cuerpo, tipo = con_avisos.avisos[0]

    assert "A-7K3QX" in asunto and "6 camisetas" in asunto and tipo == "pedido:A-7K3QX"
    assert "Pablo, Kiosco" in cuerpo and "$168.000" in cuerpo


def test_el_mismo_pedido_mandado_dos_veces_avisa_una_sola_vez(cliente, propio, con_avisos):
    cliente.post("/api/pedido", json=pedido_web())
    cliente.post("/api/pedido", json=pedido_web())

    assert len(con_avisos.avisos) == 1


def test_con_el_aviso_de_pedidos_apagado_no_sale_el_mail(cliente, propio, con_avisos):
    propio.guardar_config("avisos", {"pedidoNuevo": False})
    servidor._avisador.invalidar()

    cliente.post("/api/pedido", json=pedido_web())

    assert con_avisos.avisos == [] and propio.pedido("A-7K3QX") is not None


def test_un_pedido_se_guarda_aunque_el_aviso_falle(cliente, propio, monkeypatch):
    class Roto:
        def avisar(self, *a):
            raise OSError("sin agente")

    monkeypatch.setattr(servidor, "_avisador", servidor.avisos.Avisador(propio, Roto(), sincrono=True))

    assert cliente.post("/api/pedido", json=pedido_web()).status_code == 200
    assert propio.pedido("A-7K3QX") is not None


def test_si_tiendanube_se_cae_se_avisa_y_se_sigue_mostrando_lo_ultimo(cliente, monkeypatch, propio, con_avisos):
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto()])
    propio.cargar([(1, "M", 3)])
    cliente.get("/api/catalogo")
    servidor._cache["t"] = time.time() - servidor.CACHE_SEGUNDOS - 1

    def cae():
        raise OSError("sin conexión")

    monkeypatch.setattr(servidor, "_traer_productos", cae)

    assert len(cliente.get("/api/catalogo").json()["productos"]) == 1
    assert "no pudo leer Tiendanube" in con_avisos.avisos[0][0] and "sin conexión" in con_avisos.avisos[0][1]


def test_la_primera_revision_de_modelos_nuevos_no_avisa_y_toma_lo_que_hay_como_conocido(monkeypatch, propio, con_avisos):
    monkeypatch.setattr(servidor.tn, "TIENDA", "1")
    monkeypatch.setattr(servidor.tn, "TOKEN", "t")
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto(id=5, nombre="River")])

    servidor._revisar_modelos_nuevos()

    assert con_avisos.avisos == [] and propio.config("avisos_estado")["nuevosAvisados"] == [5]


def test_despues_avisa_solo_de_los_modelos_que_aparecen(monkeypatch, propio, con_avisos):
    monkeypatch.setattr(servidor.tn, "TIENDA", "1")
    monkeypatch.setattr(servidor.tn, "TOKEN", "t")
    tienda = [producto(id=5, nombre="River")]
    monkeypatch.setattr(servidor, "_traer_productos", lambda: tienda)
    servidor._revisar_modelos_nuevos()
    tienda.append(producto(id=6, nombre="Boca Retro"))

    servidor._revisar_modelos_nuevos()
    servidor._revisar_modelos_nuevos()  # la tercera vez ya los conoce

    assert len(con_avisos.avisos) == 1 and "Boca Retro" in con_avisos.avisos[0][1] and "River" not in con_avisos.avisos[0][1]


def test_un_modelo_ignorado_o_incluido_no_cuenta_como_nuevo(monkeypatch, propio, con_avisos):
    monkeypatch.setattr(servidor.tn, "TIENDA", "1")
    monkeypatch.setattr(servidor.tn, "TOKEN", "t")
    tienda = [producto(id=5, nombre="River")]
    monkeypatch.setattr(servidor, "_traer_productos", lambda: tienda)
    servidor._revisar_modelos_nuevos()
    tienda += [producto(id=6, nombre="Ignorado"), producto(id=7, nombre="Incluido")]
    propio.ignorar(6, "Ignorado")
    propio.cargar([(7, "M", 1)])

    servidor._revisar_modelos_nuevos()

    assert con_avisos.avisos == []


# -- Parche y número, datos de envío y cómo pagar -----------------------------------

LINEA_TOP_PERS = {"id": "top_pers", "nombre": "Top Quality con parche y número", "cats": [], "personalizadaDe": "top", "minimo": 6, "tramos": [{"desde": 6, "precio": 45000}, {"desde": 12, "precio": 42000}]}


def test_las_camisetas_con_parche_y_numero_se_cobran_en_su_propia_linea(cliente, con_lineas):
    guardar_web(con_lineas, lineas=[LINEA_TOP, LINEA_TOP_PERS, LINEA_G5], tramos=[])
    cliente.get("/api/catalogo")
    items = [{"producto": 2, "talle": "M", "cantidad": 6}, {"producto": 2, "talle": "M", "cantidad": 6, "pers": True}]

    cliente.post("/api/pedido", json=pedido_web(items=items))

    p = con_lineas.pedido("A-7K3QX")
    assert p["total"] == 6 * 35000 + 6 * 45000
    assert [bool(i.get("pers")) for i in p["datos"]["items"]] == [False, True]


def test_la_web_recibe_la_variante_con_parche_y_numero(cliente, con_lineas):
    guardar_web(con_lineas, lineas=[LINEA_TOP, LINEA_TOP_PERS, LINEA_G5], tramos=[])

    lineas = {l["id"]: l for l in cliente.get("/api/catalogo").json()["lineas"]}

    assert lineas["top_pers"]["personalizadaDe"] == "top" and lineas["top_pers"]["cats"] == []


def test_el_pedido_guarda_los_datos_de_envio_y_el_panel_los_ve(cliente, propio):
    envio = {"dni": " 30123456 ", "provincia": "Santa Fe", "ciudad": "Rosario", "cp": "2000", "direccion": "Mitre 123", "email": "pablo@correo.com", "transporte": "Vía Cargo"}

    cliente.post("/api/pedido", json=pedido_web(envio=envio))

    assert propio.pedido("A-7K3QX")["datos"]["envio"] == {**envio, "dni": "30123456"}


def test_un_pedido_sin_datos_de_envio_se_guarda_igual(cliente, propio):
    cliente.post("/api/pedido", json=pedido_web())

    assert "envio" not in propio.pedido("A-7K3QX")["datos"]


def test_la_web_sabe_si_pide_envio_y_como_se_paga(cliente, monkeypatch, propio):
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto()])
    d = cliente.get("/api/catalogo").json()

    assert d["pedirEnvio"] is True
    assert "comprobante" in d["pago"]


def test_se_puede_dejar_de_pedir_el_envio_y_cambiar_el_texto_de_pago(cliente, monkeypatch, propio):
    monkeypatch.setattr(servidor, "_traer_productos", lambda: [producto()])
    guardar_web(propio, pedirEnvio=False, pago="Alias SF.MAYORISTA")

    d = cliente.get("/api/catalogo").json()

    assert (d["pedirEnvio"], d["pago"]) == (False, "Alias SF.MAYORISTA")


def test_no_hay_documentacion_publica_de_las_rutas(cliente):
    """/openapi.json listaba todas las rutas del panel."""
    for ruta in ("/docs", "/redoc", "/openapi.json"):
        assert cliente.get(ruta).status_code == 404


def test_cabeceras_de_seguridad(cliente):
    web = cliente.get("/salud", headers={"x-forwarded-proto": "https"})
    assert web.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'self'" in web.headers["content-security-policy"]
    assert web.headers["strict-transport-security"].startswith("max-age=")
    panel = cliente.get("/admin/api/estado")
    assert "frame-ancestors 'none'" in panel.headers["content-security-policy"]
    assert panel.headers["x-frame-options"] == "DENY"
    # por http no se manda HSTS
    assert "strict-transport-security" not in cliente.get("/salud").headers
