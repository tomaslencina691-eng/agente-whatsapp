"""El alta de un producto nuevo desde el panel: se crea en Tiendanube y entra solo al mayorista.

Tiendanube es de mentira (en memoria). Lo importante:

  · El cuerpo que se manda es el que pide la API: una variante «Talle» por talle,
    con el stock contado, el precio y las categorías.
  · Todo o nada: si una foto no sube, el producto se borra de la tienda.
  · Lo creado entra al mayorista con el stock de cada talle y queda en el historial.
  · Un modelo oculto se publica desde la planilla.
"""

from __future__ import annotations

import base64

import pytest

from test_mayorista_admin import H, TiendaFalsa, admin, tn  # noqa: F401  (deja mayorista en sys.path)
from test_mayorista_admin import agente, alm, avisador, cliente, invalidaciones, memoria, sesion  # noqa: F401  (fixtures)

FOTO = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff" + b"0" * 100).decode()


def _datos(**extra):
    datos = {
        "nombre": "Boca Juniors Titular 26/27",
        "descripcion": "Tela liviana.\n\nEscudo bordado.",
        "categorias": [5, 6],
        "precio": 45000,
        "talles": [{"talle": "S", "stock": 2}, {"talle": "M", "stock": 5, "sku": "BOCA-M"}, {"talle": "L", "stock": 0}],
        "fotos": [{"datos": FOTO}, {"datos": FOTO}],
    }
    datos.update(extra)
    return datos


class TiendaQueCrea(TiendaFalsa):
    """La tienda de mentira, que además crea productos y recibe fotos."""

    def __init__(self):
        super().__init__()
        self.creados: list[dict] = []
        self.fotos: list[tuple] = []
        self.borrados: list[int] = []
        self.publicados: list[int] = []
        self.falla_la_foto: int | None = None

    def instalar(self, monkeypatch):
        super().instalar(monkeypatch)
        monkeypatch.setattr(tn, "crear_producto", self.crear)
        monkeypatch.setattr(tn, "subir_foto", self.subir_foto)
        monkeypatch.setattr(tn, "borrar_producto", self.borrar)
        monkeypatch.setattr(tn, "publicar", self.publicar)

    def crear(self, cuerpo):
        self.creados.append(cuerpo)
        id = 100 + len(self.creados)
        p = {
            "id": id,
            "name": cuerpo["name"],
            "published": cuerpo["published"],
            "categories": [{"id": c, "name": {"es": "X"}} for c in cuerpo.get("categories", [])],
            "images": [],
            "variants": [{"id": id * 10 + i, **v} for i, v in enumerate(cuerpo["variants"])],
        }
        self.productos[id] = p
        return p

    def subir_foto(self, producto, b64, nombre, posicion):
        if self.falla_la_foto == posicion:
            raise tn.ErrorDeTiendanube("Tiendanube devolvió 422 en POST /images: imagen inválida")
        self.fotos.append((producto, nombre, posicion))
        self.productos[producto]["images"].append({"position": posicion, "src": f"https://acdn-us.mitiendanube.com/x/{nombre}"})
        return {"id": len(self.fotos)}

    def borrar(self, producto):
        self.borrados.append(producto)
        self.productos.pop(producto, None)

    def publicar(self, producto, publicado=True):
        self.publicados.append(producto)
        self.productos[producto]["published"] = publicado
        return self.productos[producto]


@pytest.fixture
def tienda(monkeypatch):  # noqa: F811  (reemplaza la de test_mayorista_admin)
    t = TiendaQueCrea()
    t.instalar(monkeypatch)
    return t


# -- Lo que se le manda a Tiendanube ------------------------------------------------


def test_el_cuerpo_tiene_una_variante_por_talle_con_su_stock():
    cuerpo = admin.cuerpo_para_tiendanube(admin.ProductoNuevo(**_datos(precioPromocional=40000, peso=0.3, marca="Adidas", etiquetas="boca, titular")))

    assert cuerpo["name"] == {"es": "Boca Juniors Titular 26/27"}
    assert cuerpo["attributes"] == [{"es": "Talle"}]
    assert cuerpo["published"] is False, "por defecto se crea oculto"
    assert cuerpo["categories"] == [5, 6]
    assert cuerpo["description"]["es"] == "<p>Tela liviana.</p><p>Escudo bordado.</p>"
    assert cuerpo["brand"] == "Adidas" and cuerpo["tags"] == "boca,titular"
    m = cuerpo["variants"][1]
    assert m["values"] == [{"es": "M"}] and m["stock"] == 5 and m["stock_management"] is True
    assert m["price"] == "45000.00" and m["promotional_price"] == "40000.00" and m["weight"] == "0.30"
    assert m["sku"] == "BOCA-M" and "sku" not in cuerpo["variants"][0]


def test_dos_talles_iguales_no_pasan():
    with pytest.raises(ValueError, match="dos talles iguales"):
        admin.cuerpo_para_tiendanube(admin.ProductoNuevo(**_datos(talles=[{"talle": "M", "stock": 1}, {"talle": " m ", "stock": 2}])))


def test_la_promocion_tiene_que_ser_menor():
    with pytest.raises(ValueError, match="menor"):
        admin.cuerpo_para_tiendanube(admin.ProductoNuevo(**_datos(precioPromocional=45000)))


def test_la_descripcion_se_escapa():
    cuerpo = admin.cuerpo_para_tiendanube(admin.ProductoNuevo(**_datos(descripcion="<script>x</script>\nsegunda")))
    assert cuerpo["description"]["es"] == "<p>&lt;script&gt;x&lt;/script&gt;<br>segunda</p>"


def test_una_foto_que_no_es_imagen_no_pasa():
    with pytest.raises(ValueError):
        admin.foto_en_base64("data:text/html;base64,PGgxPg==")
    with pytest.raises(ValueError):
        admin.foto_en_base64("data:image/png;base64,esto no es base64!")
    assert admin.foto_en_base64(FOTO)[1] == "jpg"


# -- De punta a punta, por el panel ---------------------------------------------------


def test_crear_un_producto_lo_sube_y_lo_suma_al_mayorista(sesion, tienda, alm, memoria, invalidaciones):
    r = sesion.post("/admin/api/productos", json=_datos(), headers=H)

    assert r.status_code == 200, r.text
    d = r.json()
    assert d["nombre"] == "Boca Juniors Titular 26/27" and d["publicado"] is False and d["unidades"] == 7
    assert [f[2] for f in tienda.fotos] == [1, 2], "las fotos suben en orden: la primera es la principal"
    assert [(f["talle"], f["cantidad"]) for f in alm.filas([d["id"]])] == [("S", 2), ("M", 5), ("L", 0)]
    assert memoria.filas[-1]["accion"] == "creado"
    assert invalidaciones, "la web tiene que volver a leer la tienda"


def test_si_una_foto_falla_el_producto_se_borra(sesion, tienda, alm):
    tienda.falla_la_foto = 2

    r = sesion.post("/admin/api/productos", json=_datos(), headers=H)

    assert r.status_code == 502
    assert "foto 2" in r.json()["detail"] and "No quedó nada creado" in r.json()["detail"]
    assert tienda.borrados == [101]
    assert alm.filas([101]) == [], "no entra al mayorista"


def test_si_tiendanube_no_crea_no_queda_nada(sesion, tienda, alm, monkeypatch):
    def no_crea(cuerpo):
        raise tn.ErrorDeTiendanube("Tiendanube devolvió 403: falta el permiso write_products")

    monkeypatch.setattr(tn, "crear_producto", no_crea)
    r = sesion.post("/admin/api/productos", json=_datos(), headers=H)

    assert r.status_code == 502 and "write_products" in r.json()["detail"]
    assert tienda.fotos == []


def test_sin_fotos_no_se_crea(sesion, tienda):
    r = sesion.post("/admin/api/productos", json=_datos(fotos=[]), headers=H)

    assert r.status_code == 422
    assert tienda.creados == []


def test_las_categorias_vienen_con_la_de_arriba(sesion, tienda):
    r = sesion.get("/admin/api/productos/categorias").json()

    assert {"id": 6, "nombre": "FÚTBOL ARGENTINO › BOCA"} in r["categorias"]


def test_publicar_un_modelo_oculto(sesion, tienda, memoria):
    creado = sesion.post("/admin/api/productos", json=_datos(), headers=H).json()

    r = sesion.post(f"/admin/api/productos/{creado['id']}/publicar", headers=H)

    assert r.status_code == 200
    assert tienda.publicados == [creado["id"]] and tienda.productos[creado["id"]]["published"] is True
    assert memoria.filas[-1]["talle"] == "publicado en la tienda online"


def test_no_se_publica_lo_que_no_esta_en_el_mayorista(sesion, tienda):
    r = sesion.post("/admin/api/productos/999/publicar", headers=H)

    assert r.status_code == 404
    assert tienda.publicados == []


def test_el_alta_pide_sesion_y_el_header(cliente, sesion, tienda):
    assert sesion.post("/admin/api/productos", json=_datos()).status_code == 403, "sin el header propio no se crea nada"
    sesion.post("/admin/api/logout", headers=H)
    sesion.cookies.clear()
    assert sesion.post("/admin/api/productos", json=_datos(), headers=H).status_code == 401
    assert tienda.creados == []


# -- Sacar de la planilla un modelo que borraron de la tienda --------------------------


def test_quitar_un_modelo_borrado_de_la_tienda(sesion, tienda, alm, memoria):
    creado = sesion.post("/admin/api/productos", json=_datos(), headers=H).json()
    tienda.borrar(creado["id"])  # lo borran desde Tiendanube

    planilla = {p["id"]: p for p in sesion.get("/admin/api/stock").json()["productos"]}
    assert planilla[creado["id"]]["enTienda"] is False

    r = sesion.post(f"/admin/api/productos/{creado['id']}/quitar", headers=H)

    assert r.status_code == 200
    assert alm.filas([creado["id"]]) == []
    assert memoria.filas[-1]["accion"] == "quitado"


def test_no_se_quita_un_modelo_que_sigue_en_la_tienda(sesion, tienda, alm):
    r = sesion.post("/admin/api/productos/1/quitar", headers=H)

    assert r.status_code == 409
    assert alm.filas([1]), "el modelo 1 sigue en la planilla"


# -- Revisión de precios en el panel -----------------------------------------------------


def test_la_revision_lista_los_modelos_con_dorsal(sesion, tienda):
    tienda.productos[1]["name"] = {"es": "Boca Juniors - Titular 26/27 – Con Dorsal"}

    r = sesion.get("/admin/api/web/revision-precios")

    assert r.status_code == 200
    assert [m["id"] for m in r.json()["personalizados"]] == [1], "el 2 está oculto en la tienda"
    assert r.json()["dobles"] == []
