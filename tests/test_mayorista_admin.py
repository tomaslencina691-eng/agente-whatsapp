"""Pruebas del panel de administración del mayorista. No salen a internet ni usan base.

Tiendanube se reemplaza por una tienda de mentira en memoria y la base por
`AlmacenMemoria`: lo que se prueba es lo nuestro. Lo más importante de este archivo:

  · El stock del mayorista es PROPIO: tocarlo no cambia la tienda, y una venta en
    la tienda no lo cambia a él.
  · `test_un_conflicto_no_aplica_ni_los_cambios_buenos`: si entra un pedido
    confirmado mientras alguien edita la planilla, no se pisa el stock.
  · Confirmar un pedido que no alcanza no descuenta nada, ni de lo que sí había.
  · Todo lo que no es el login exige sesión.

    pytest
"""

from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mayorista"))

import acceso  # noqa: E402
import admin  # noqa: E402
import agente_cliente  # noqa: E402
import ajustes  # noqa: E402
import almacen  # noqa: E402
import avisos  # noqa: E402
import historial as hist  # noqa: E402
import tn  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

CLAVE = "clave-de-prueba-123"
DEFECTO_WEB = ajustes.por_defecto({}, "Mi Tienda", "5491100000000", "https://tienda.example", True, 6, [{"desde": 6, "precio": 28000}, {"desde": 12, "precio": 25000}])
PNG_LOGO = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 50).decode()
FILTROS = [{"id": "arg", "nombre": "Fútbol argentino", "tiendanube": ["FÚTBOL ARGENTINO"]}]  # lo que diría negocio.json
H = {"x-mayorista": "1"}


class TiendaFalsa:
    """Una tienda en memoria con la forma que devuelve la API de Tiendanube (solo lectura)."""

    def __init__(self):
        self.productos = {
            1: self._producto(1, "Boca Juniors - Titular 26/27", True, {"S": 0, "M": 2, "L": 3}),
            2: self._producto(2, "Argentina Retro", False, {"XL": 0}),  # oculto y sin stock
            3: self._producto(3, "River Plate - Titular 26/27", True, {"S": 1, "M": 4}),  # nuevo: la tienda lo tiene, el mayorista no
            4: self._producto(4, "Sin foto", True, {"M": 5}, fotos=False),
            5: self._producto(5, "Oculto nuevo", False, {"M": 1}),  # sin publicar y todavía no está en el mayorista
            6: self._producto(6, "Sin cuenta de stock", True, {"M": None}),  # la tienda no cuenta su stock
        }

    @staticmethod
    def _producto(id, nombre, publicado, talles, fotos=True):
        return {
            "id": id,
            "name": {"es": nombre},
            "published": publicado,
            "categories": [{"name": {"es": "FÚTBOL ARGENTINO"}}],
            "images": [{"position": 1, "src": "https://acdn-us.mitiendanube.com/x/a-1024-1024.png"}] if fotos else [],
            "variants": [
                {"id": id * 10 + i, "price": "75000.00", "stock": s, "stock_management": True, "values": [{"es": t}]}
                for i, (t, s) in enumerate(talles.items())
            ],
        }

    def vender(self, producto, talle, n=1):
        for v in self.productos[producto]["variants"]:
            if v["values"][0]["es"] == talle:
                v["stock"] -= n

    def stock(self, producto, talle):
        return next(v["stock"] for v in self.productos[producto]["variants"] if v["values"][0]["es"] == talle)

    def instalar(self, monkeypatch):
        monkeypatch.setattr(tn, "TIENDA", "1")
        monkeypatch.setattr(tn, "TOKEN", "token")
        monkeypatch.setattr(tn, "todos_los_productos", lambda: list(self.productos.values()))
        monkeypatch.setattr(tn, "productos_por_id", lambda ids: {i: self.productos[i] for i in ids if i in self.productos})
        monkeypatch.setattr(tn, "categorias", lambda: [{"id": 5, "name": {"es": "FÚTBOL ARGENTINO"}, "parent": None}, {"id": 6, "name": {"es": "BOCA"}, "parent": 5}])


class AgenteFalso:
    """El agente de verdad vive en otro contenedor: acá contesta lo que se le diga."""

    def __init__(self):
        self.caido = False
        self.avisos = []
        self.pruebas = []
        self.estado_ = {
            "proveedor": "openai", "modelo": "gpt-5", "prompt_base": "Sos el asistente de la tienda. Atendés por WhatsApp.",
            "base": {"activo": True, "mensajesPorRespuesta": 1, "topeMensajesPorDia": 10, "respetarResueltas": True, "soloElPrimerMensaje": False, "avisosEmail": "duenio@ejemplo.com"},
            "vivos": {}, "variables": list(admin.VARIABLES_DEL_AGENTE),
            "conexiones": {"chatwoot": True, "whatsapp": False, "avisos": True, "avisos_para": "duenio@ejemplo.com"},
        }

    def estado(self):
        if self.caido:
            raise agente_cliente.ErrorDelAgente("No se pudo hablar con el agente: connection refused")
        return self.estado_

    def avisar(self, asunto, cuerpo, tipo):
        self.avisos.append((asunto, cuerpo, tipo))
        return {"enviado": True, "activos": True}

    def mail_de_prueba(self):
        return {"ok": True, "para": "duenio@ejemplo.com"}

    def probar(self, prompt, mensajes, datos):
        if self.caido:
            raise agente_cliente.ErrorDelAgente("El modelo no contestó")
        self.pruebas.append((prompt, mensajes, datos))
        return {"texto": "Hola, ¿qué talle buscás?", "mensajes": ["Hola, ¿qué talle buscás?"], "herramientas": [], "tokens_entrada": 10, "tokens_salida": 5}


@pytest.fixture
def agente():
    return AgenteFalso()


@pytest.fixture
def avisador(alm, agente):
    """Los avisos por mail, sin hilos: cada aviso queda anotado en el agente de mentira."""
    return avisos.Avisador(alm, agente, sincrono=True)


@pytest.fixture
def tienda(monkeypatch):
    t = TiendaFalsa()
    t.instalar(monkeypatch)
    return t


@pytest.fixture
def memoria():
    return hist.HistorialMemoria()


@pytest.fixture
def alm():
    """El mayorista arranca con los modelos 1 y 2 (copiados de la tienda): el 3 todavía no está."""
    a = almacen.AlmacenMemoria()
    a.cargar([(1, "S", 0), (1, "M", 2), (1, "L", 3), (2, "XL", 0)])
    return a


@pytest.fixture
def invalidaciones():
    return []


@pytest.fixture
def cliente(monkeypatch, tienda, memoria, alm, invalidaciones, agente, avisador):
    monkeypatch.setattr(acceso, "USUARIO", "leo")
    monkeypatch.setattr(acceso, "CLAVE", CLAVE)
    monkeypatch.setattr(acceso, "SECRETO", "s" * 40)
    acceso._fallos.clear()
    app = FastAPI()
    app.include_router(admin.crear_router(memoria, lambda: invalidaciones.append(1), alm, FILTROS, lambda: dict(DEFECTO_WEB), agente, avisador))
    return TestClient(app)


@pytest.fixture
def sesion(cliente):
    r = cliente.post("/admin/api/login", json={"usuario": "leo", "clave": CLAVE}, headers=H)
    assert r.status_code == 200
    return cliente


def fila(alm, producto, talle):
    return next(f for f in alm.filas([producto]) if f["talle"] == talle)


def hay(alm, producto, talle):
    return fila(alm, producto, talle)["cantidad"]


def cambio(alm, producto, talle, despues, antes=None):
    f = fila(alm, producto, talle)
    return {"tipo": "stock", "producto": producto, "variante": f["id"], "antes": f["cantidad"] if antes is None else antes, "despues": despues}


def pedido(alm, codigo, *items, estado="nuevo"):
    """Un pedido de la web: items = (modelo, talle, cantidad)."""
    datos = {"items": [{"producto": p, "talle": t, "cantidad": n, "nombre": "Boca Juniors - Titular 26/27"} for p, t, n in items], "nombre": "Pablo", "negocio": "Kiosco", "notas": ""}
    alm.crear_pedido(codigo, datos, sum(n for _, _, n in items), 100000)
    if estado != "nuevo":
        alm._pedidos[codigo]["estado"] = estado


# -- Entrar -------------------------------------------------------------------


@pytest.mark.parametrize(
    "metodo, ruta",
    [
        ("get", "/admin/api/stock"), ("get", "/admin/api/historial"), ("get", "/admin/api/pedidos"), ("get", "/admin/api/tienda/nuevos"),
        ("post", "/admin/api/stock/aplicar"), ("post", "/admin/api/pedidos/A-AAAAA/confirmar"), ("post", "/admin/api/tienda/incluir"),
        ("post", "/admin/api/tienda/cargar-stock"), ("post", "/admin/api/tienda/ignorar"), ("post", "/admin/api/tienda/restaurar"),
        ("get", "/admin/api/tienda/categorias"), ("put", "/admin/api/tienda/categorias"),
        ("get", "/admin/api/web"), ("put", "/admin/api/web"), ("put", "/admin/api/web/logo"), ("delete", "/admin/api/web/logo"),
        ("get", "/admin/api/cuenta"), ("post", "/admin/api/cuenta/clave"), ("post", "/admin/api/usuarios"), ("put", "/admin/api/usuarios/pablo"),
        ("delete", "/admin/api/usuarios/pablo"), ("get", "/admin/api/avisos"), ("put", "/admin/api/avisos"), ("post", "/admin/api/avisos/probar"),
        ("get", "/admin/api/agente"), ("put", "/admin/api/agente/prompt"), ("post", "/admin/api/agente/prompt/restaurar"), ("put", "/admin/api/agente/ajustes"),
        ("put", "/admin/api/agente/datos"), ("post", "/admin/api/agente/probar"), ("get", "/admin/api/agente/versiones/1"),
    ],
)
def test_sin_sesion_no_se_entra_a_nada(cliente, metodo, ruta):
    r = getattr(cliente, metodo)(ruta, headers=H, **({"json": {}} if metodo in ("post", "put") else {}))

    assert r.status_code == 401


def test_con_la_clave_correcta_se_entra(cliente):
    r = cliente.post("/admin/api/login", json={"usuario": "leo", "clave": CLAVE}, headers=H)

    assert r.status_code == 200
    assert "httponly" in r.headers["set-cookie"].lower()
    assert cliente.get("/admin/api/estado").json() == {"configurado": True, "usuario": "leo", "rol": "duenio", "permisos": ["stock", "web", "agente", "cuenta", "administrar"]}


@pytest.mark.parametrize("usuario, clave", [("leo", "mala"), ("otro", CLAVE), ("", ""), ("LEO", CLAVE)])
def test_con_datos_equivocados_no_se_entra(cliente, usuario, clave):
    r = cliente.post("/admin/api/login", json={"usuario": usuario, "clave": clave}, headers=H)

    assert r.status_code == 401
    assert cliente.get("/admin/api/estado").json()["usuario"] is None


def test_tras_cinco_fallos_se_bloquea_aunque_la_clave_sea_buena(cliente):
    for _ in range(5):
        cliente.post("/admin/api/login", json={"usuario": "leo", "clave": "mala"}, headers=H)

    r = cliente.post("/admin/api/login", json={"usuario": "leo", "clave": CLAVE}, headers=H)

    assert r.status_code == 429


def test_el_bloqueo_se_levanta_con_el_tiempo():
    acceso._fallos.clear()
    for _ in range(5):
        acceso.anotar_fallo("1.2.3.4", ahora=1000.0)

    assert acceso.bloqueado("1.2.3.4", ahora=1100.0)
    assert not acceso.bloqueado("1.2.3.4", ahora=1000.0 + acceso.VENTANA_SEGUNDOS + 1)


def test_salir_borra_la_sesion(sesion):
    sesion.post("/admin/api/logout", headers=H)

    assert sesion.get("/admin/api/stock").status_code == 401


def test_una_cookie_adulterada_no_sirve(cliente):
    valida = acceso.crear_sesion("leo")
    cuerpo, firma = valida.rsplit(".", 1)

    assert acceso.leer_sesion(valida) == "leo"
    assert acceso.leer_sesion(cuerpo + "." + "0" * len(firma)) is None
    assert acceso.leer_sesion("basura") is None
    assert acceso.leer_sesion(None) is None


def test_una_sesion_vencida_no_sirve():
    vieja = acceso.crear_sesion("leo", ahora=time.time() - acceso.DURACION_SEGUNDOS - 10)

    assert acceso.leer_sesion(vieja) is None


def test_un_secreto_corto_apaga_el_panel(cliente, monkeypatch):
    """Sin un secreto largo las cookies se podrían falsificar: mejor no tener panel."""
    monkeypatch.setattr(acceso, "SECRETO", "corto")

    assert cliente.get("/admin/api/estado").json()["configurado"] is False
    assert cliente.post("/admin/api/login", json={"usuario": "leo", "clave": CLAVE}, headers=H).status_code == 503


def test_los_cambios_exigen_el_header_propio(sesion, alm):
    """Es lo que impide que otra web mande cambios desde tu navegador."""
    r = sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(alm, 1, "M", 5)]})

    assert r.status_code == 403
    assert hay(alm, 1, "M") == 2


# -- Ver ----------------------------------------------------------------------


def test_la_planilla_trae_solo_los_modelos_del_mayorista_con_su_stock_propio(sesion):
    r = sesion.get("/admin/api/stock")

    productos = {p["id"]: p for p in r.json()["productos"]}
    assert set(productos) == {1, 2}, "el modelo 3 está en la tienda pero todavía no en el mayorista"
    assert productos[2]["publicado"] is False
    assert [(x["talle"], x["stock"]) for x in productos[1]["variantes"]] == [("S", 0), ("M", 2), ("L", 3)]
    assert r.json()["vacio"] is False


def test_el_stock_es_propio_una_venta_en_la_tienda_no_lo_cambia(sesion, tienda, alm):
    tienda.vender(1, "M", 2)

    productos = {p["id"]: p for p in sesion.get("/admin/api/stock").json()["productos"]}

    assert tienda.stock(1, "M") == 0
    assert hay(alm, 1, "M") == 2
    assert [x["stock"] for x in productos[1]["variantes"] if x["talle"] == "M"] == [2]


def test_un_modelo_borrado_de_la_tienda_sigue_en_la_planilla(sesion, tienda):
    del tienda.productos[2]

    productos = {p["id"]: p for p in sesion.get("/admin/api/stock").json()["productos"]}

    assert "ya no está en la tienda" in productos[2]["nombre"]


# -- Guardar ------------------------------------------------------------------


def test_guardar_cambia_el_stock_propio_y_lo_anota(sesion, tienda, alm, memoria):
    r = sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(alm, 1, "M", 7), cambio(alm, 1, "S", 4)]}, headers=H)

    assert r.status_code == 200
    assert r.json()["aplicados"] == 2
    assert (hay(alm, 1, "M"), hay(alm, 1, "S")) == (7, 4)
    assert tienda.stock(1, "M") == 2, "la tienda no se toca"
    assert {(e["talle"], e["antes"], e["despues"]) for e in memoria.filas} == {("M", 2, 7), ("S", 0, 4)}
    assert memoria.filas[0]["usuario"] == "leo"


def test_un_conflicto_no_aplica_ni_los_cambios_buenos(sesion, alm, memoria):
    """Se confirmó un pedido mientras editabas: no se pisa, y no se guarda nada."""
    mios = [cambio(alm, 1, "M", 9), cambio(alm, 1, "L", 8)]
    pedido(alm, "A-AAAAA", (1, "M", 1))
    alm.confirmar("A-AAAAA")  # entra un pedido en el medio

    r = sesion.post("/admin/api/stock/aplicar", json={"cambios": mios}, headers=H)

    assert r.status_code == 409
    conflicto = r.json()["detail"]["conflictos"][0]
    assert (conflicto["talle"], conflicto["esperado"], conflicto["actual"]) == ("M", 2, 1)
    assert hay(alm, 1, "L") == 3, "el cambio bueno tampoco se aplicó"
    assert memoria.filas == []


def test_si_la_base_detecta_el_choque_justo_al_guardar_tampoco_se_aplica(sesion, alm, monkeypatch):
    """Dos personas guardando a la vez: la segunda se entera en vez de pisar a la primera."""
    original = alm.aplicar

    def con_choque(cambios, altas):
        f = fila(alm, 1, "M")
        f["cantidad"] = 99  # alguien se adelantó entre la lectura y la escritura
        alm._stock[f["id"]]["cantidad"] = 99
        return original(cambios, altas)

    monkeypatch.setattr(alm, "aplicar", con_choque)

    r = sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(alm, 1, "M", 5)]}, headers=H)

    assert r.status_code == 409
    assert hay(alm, 1, "M") == 99


def test_no_se_guarda_stock_negativo(sesion, alm):
    r = sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(alm, 1, "M", -1)]}, headers=H)

    assert r.status_code == 422
    assert hay(alm, 1, "M") == 2


def test_guardar_sin_cambios_reales_no_hace_nada(sesion, alm, memoria):
    r = sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(alm, 1, "M", 2)]}, headers=H)

    assert r.status_code == 200
    assert r.json()["lote"] is None
    assert memoria.filas == []


def test_un_talle_de_otro_modelo_no_se_toca(sesion, alm):
    ajeno = cambio(alm, 2, "XL", 5)
    ajeno["producto"] = 1  # la fila es del modelo 2, el cambio dice modelo 1

    r = sesion.post("/admin/api/stock/aplicar", json={"cambios": [ajeno]}, headers=H)

    assert r.status_code == 409
    assert hay(alm, 2, "XL") == 0


def test_un_talle_nuevo_se_crea_solo_en_el_mayorista(sesion, tienda, alm, memoria):
    r = sesion.post("/admin/api/stock/aplicar", json={"cambios": [{"tipo": "talle_nuevo", "producto": 1, "talle": "XXL", "despues": 3}]}, headers=H)

    assert r.status_code == 200
    assert r.json()["creados"] == 1
    assert hay(alm, 1, "XXL") == 3
    assert len(tienda.productos[1]["variants"]) == 3, "la tienda no recibe el talle"
    assert memoria.filas[0]["accion"] == "talle_nuevo"
    assert memoria.filas[0]["variante_id"] == fila(alm, 1, "XXL")["id"]


def test_un_talle_que_ya_existe_no_se_duplica(sesion):
    r = sesion.post("/admin/api/stock/aplicar", json={"cambios": [{"tipo": "talle_nuevo", "producto": 1, "talle": " m ", "despues": 3}]}, headers=H)

    assert r.status_code == 409


def test_no_se_crean_talles_en_un_modelo_que_no_esta_en_el_mayorista(sesion, alm):
    r = sesion.post("/admin/api/stock/aplicar", json={"cambios": [{"tipo": "talle_nuevo", "producto": 3, "talle": "M", "despues": 3}]}, headers=H)

    assert r.status_code == 409
    assert alm.filas([3]) == []


# -- Deshacer -----------------------------------------------------------------


def test_deshacer_devuelve_el_stock_de_antes(sesion, alm):
    lote = sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(alm, 1, "M", 7), cambio(alm, 1, "L", 9)]}, headers=H).json()["lote"]

    r = sesion.post(f"/admin/api/historial/{lote}/deshacer", headers=H)

    assert r.status_code == 200
    assert (hay(alm, 1, "M"), hay(alm, 1, "L")) == (2, 3)


def test_no_se_deshace_dos_veces(sesion, alm):
    lote = sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(alm, 1, "M", 7)]}, headers=H).json()["lote"]
    sesion.post(f"/admin/api/historial/{lote}/deshacer", headers=H)

    assert sesion.post(f"/admin/api/historial/{lote}/deshacer", headers=H).status_code == 404


def test_si_el_stock_cambio_despues_no_se_deshace(sesion, alm):
    """Deshacer un guardado de ayer no puede pisar los pedidos de hoy."""
    lote = sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(alm, 1, "M", 7)]}, headers=H).json()["lote"]
    pedido(alm, "A-AAAAA", (1, "M", 1))
    alm.confirmar("A-AAAAA")

    r = sesion.post(f"/admin/api/historial/{lote}/deshacer", headers=H)

    assert r.status_code == 409
    assert hay(alm, 1, "M") == 6


def test_deshacer_un_talle_nuevo_lo_deja_en_cero(sesion, alm):
    lote = sesion.post("/admin/api/stock/aplicar", json={"cambios": [{"tipo": "talle_nuevo", "producto": 1, "talle": "XXL", "despues": 3}]}, headers=H).json()["lote"]

    sesion.post(f"/admin/api/historial/{lote}/deshacer", headers=H)

    assert hay(alm, 1, "XXL") == 0


def test_el_historial_agrupa_por_guardado(sesion, alm):
    sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(alm, 1, "M", 7), cambio(alm, 1, "L", 9)]}, headers=H)

    lotes = sesion.get("/admin/api/historial").json()

    assert len(lotes) == 1 and len(lotes[0]["entradas"]) == 2
    assert lotes[0]["se_puede_deshacer"] is True


# -- Pedidos ------------------------------------------------------------------


def test_los_pedidos_se_listan_con_lo_que_hay_ahora(sesion, alm):
    pedido(alm, "A-AAAAA", (1, "M", 2), (1, "L", 5))

    r = sesion.get("/admin/api/pedidos").json()

    p = r["pedidos"][0]
    assert (p["codigo"], p["estado"], p["unidades"], p["nombre"], p["negocio"]) == ("A-AAAAA", "nuevo", 7, "Pablo", "Kiosco")
    assert [(i["talle"], i["cantidad"], i["hay"]) for i in p["items"]] == [("M", 2, 2), ("L", 5, 3)]
    assert p["falta"] is True, "pidió 5 de una L y hay 3"
    assert r["cuenta"] == {"nuevo": 1}


def test_un_estado_desconocido_no_se_acepta(sesion):
    assert sesion.get("/admin/api/pedidos?estado=cualquiera").status_code == 422


def test_confirmar_descuenta_el_stock_y_lo_anota(sesion, alm, memoria):
    pedido(alm, "A-AAAAA", (1, "M", 2), (1, "L", 1))

    r = sesion.post("/admin/api/pedidos/A-AAAAA/confirmar", headers=H)

    assert r.status_code == 200
    assert r.json() == {"codigo": "A-AAAAA", "estado": "confirmado", "unidades": 3}
    assert (hay(alm, 1, "M"), hay(alm, 1, "L")) == (0, 2)
    assert alm.pedido("A-AAAAA")["estado"] == "confirmado"
    assert {(e["accion"], e["talle"], e["antes"], e["despues"]) for e in memoria.filas} == {("pedido", "M", 2, 0), ("pedido", "L", 3, 2)}
    assert "A-AAAAA" in memoria.filas[0]["producto"]


def test_si_no_alcanza_no_se_descuenta_nada_ni_de_lo_que_si_habia(sesion, alm, memoria):
    pedido(alm, "A-AAAAA", (1, "M", 1), (1, "L", 5))  # de la L hay 3

    r = sesion.post("/admin/api/pedidos/A-AAAAA/confirmar", headers=H)

    assert r.status_code == 409
    assert r.json()["detail"]["conflictos"][0]["talle"] == "L"
    assert (hay(alm, 1, "M"), hay(alm, 1, "L")) == (2, 3)
    assert alm.pedido("A-AAAAA")["estado"] == "nuevo"
    assert memoria.filas == []


def test_las_lineas_repetidas_del_mismo_talle_se_suman_antes_de_confirmar(sesion, alm):
    pedido(alm, "A-AAAAA", (1, "M", 2), (1, "m", 1))  # 3 en total y hay 2

    assert sesion.post("/admin/api/pedidos/A-AAAAA/confirmar", headers=H).status_code == 409
    assert hay(alm, 1, "M") == 2


def test_un_pedido_no_se_confirma_dos_veces(sesion, alm):
    pedido(alm, "A-AAAAA", (1, "M", 1))
    sesion.post("/admin/api/pedidos/A-AAAAA/confirmar", headers=H)

    r = sesion.post("/admin/api/pedidos/A-AAAAA/confirmar", headers=H)

    assert r.status_code == 409
    assert hay(alm, 1, "M") == 1, "no se descontó de nuevo"


def test_confirmar_un_pedido_que_no_existe_da_404(sesion):
    assert sesion.post("/admin/api/pedidos/A-ZZZZZ/confirmar", headers=H).status_code == 404


def test_cancelar_un_pedido_confirmado_devuelve_el_stock(sesion, alm, memoria):
    pedido(alm, "A-AAAAA", (1, "M", 2))
    sesion.post("/admin/api/pedidos/A-AAAAA/confirmar", headers=H)

    r = sesion.post("/admin/api/pedidos/A-AAAAA/cancelar", headers=H)

    assert r.status_code == 200 and r.json()["devuelto"] is True
    assert hay(alm, 1, "M") == 2
    assert alm.pedido("A-AAAAA")["estado"] == "cancelado"
    assert memoria.filas[-1]["accion"] == "pedido_cancelado"


def test_cancelar_un_pedido_nuevo_no_toca_el_stock(sesion, alm):
    pedido(alm, "A-AAAAA", (1, "M", 2))

    r = sesion.post("/admin/api/pedidos/A-AAAAA/cancelar", headers=H)

    assert r.status_code == 200 and r.json()["devuelto"] is False
    assert hay(alm, 1, "M") == 2


def test_un_pedido_cancelado_no_se_puede_confirmar(sesion, alm):
    pedido(alm, "A-AAAAA", (1, "M", 1))
    sesion.post("/admin/api/pedidos/A-AAAAA/cancelar", headers=H)

    assert sesion.post("/admin/api/pedidos/A-AAAAA/confirmar", headers=H).status_code == 409
    assert hay(alm, 1, "M") == 2


def test_solo_se_despacha_un_pedido_confirmado(sesion, alm):
    pedido(alm, "A-AAAAA", (1, "M", 1))

    assert sesion.post("/admin/api/pedidos/A-AAAAA/despachar", headers=H).status_code == 409
    sesion.post("/admin/api/pedidos/A-AAAAA/confirmar", headers=H)
    assert sesion.post("/admin/api/pedidos/A-AAAAA/despachar", headers=H).json()["estado"] == "despachado"
    assert sesion.post("/admin/api/pedidos/A-AAAAA/cancelar", headers=H).status_code == 409, "uno despachado ya no se cancela acá"


def test_los_pedidos_se_filtran_por_estado(sesion, alm):
    pedido(alm, "A-AAAAA", (1, "M", 1))
    pedido(alm, "A-BBBBB", (1, "L", 1), estado="confirmado")

    assert [p["codigo"] for p in sesion.get("/admin/api/pedidos?estado=nuevo").json()["pedidos"]] == ["A-AAAAA"]
    assert [p["codigo"] for p in sesion.get("/admin/api/pedidos?estado=confirmado").json()["pedidos"]] == ["A-BBBBB"]
    assert len(sesion.get("/admin/api/pedidos?estado=todos").json()["pedidos"]) == 2


# -- Modelos de la tienda -----------------------------------------------------


def test_los_modelos_nuevos_son_los_de_la_tienda_que_el_mayorista_no_tiene(sesion):
    r = sesion.get("/admin/api/tienda/nuevos").json()

    assert [n["id"] for n in r["nuevos"]] == [3], "el 4 no tiene foto y el 2 ya está incluido"
    assert r["nuevos"][0]["unidades"] == 5
    assert [(t["talle"], t["stock"]) for t in r["nuevos"][0]["talles"]] == [("S", 1), ("M", 4)]


def test_incluir_un_modelo_con_el_stock_en_cero(sesion, alm, invalidaciones, memoria):
    r = sesion.post("/admin/api/tienda/incluir", json={"producto": 3, "copiar_stock": False}, headers=H)

    assert r.status_code == 200
    assert [(f["talle"], f["cantidad"]) for f in alm.filas([3])] == [("S", 0), ("M", 0)]
    assert invalidaciones, "la web tiene que volver a leer los nombres y fotos"
    assert memoria.filas[0]["accion"] == "incluido"


def test_incluir_un_modelo_copiando_el_stock_de_la_tienda(sesion, alm):
    sesion.post("/admin/api/tienda/incluir", json={"producto": 3, "copiar_stock": True}, headers=H)

    assert [(f["talle"], f["cantidad"]) for f in alm.filas([3])] == [("S", 1), ("M", 4)]


def test_incluir_no_toca_la_tienda_ni_se_vuelve_a_copiar_solo(sesion, tienda, alm):
    sesion.post("/admin/api/tienda/incluir", json={"producto": 3, "copiar_stock": True}, headers=H)
    tienda.vender(3, "M", 4)

    assert tienda.stock(3, "M") == 0
    assert hay(alm, 3, "M") == 4, "después de incluirlo, el stock del mayorista es solo suyo"


def test_un_modelo_no_se_incluye_dos_veces(sesion):
    sesion.post("/admin/api/tienda/incluir", json={"producto": 3}, headers=H)

    assert sesion.post("/admin/api/tienda/incluir", json={"producto": 3}, headers=H).status_code == 409


@pytest.mark.parametrize("producto", [2, 4, 999])
def test_no_se_incluye_lo_que_no_esta_publicado_o_no_tiene_foto(sesion, producto):
    r = sesion.post("/admin/api/tienda/incluir", json={"producto": producto}, headers=H)

    assert r.status_code in (409, 422)


def test_la_carga_inicial_solo_corre_con_el_stock_vacio(sesion):
    r = sesion.post("/admin/api/tienda/cargar-stock", headers=H)

    assert r.status_code == 409


def test_la_carga_inicial_copia_lo_elegible_de_la_tienda(monkeypatch, tienda, memoria, invalidaciones):
    vacio = almacen.AlmacenMemoria()
    monkeypatch.setattr(acceso, "USUARIO", "leo")
    monkeypatch.setattr(acceso, "CLAVE", CLAVE)
    monkeypatch.setattr(acceso, "SECRETO", "s" * 40)
    acceso._fallos.clear()
    app = FastAPI()
    app.include_router(admin.crear_router(memoria, lambda: invalidaciones.append(1), vacio))
    c = TestClient(app)
    c.post("/admin/api/login", json={"usuario": "leo", "clave": CLAVE}, headers=H)

    assert c.get("/admin/api/stock").json()["vacio"] is True
    r = c.post("/admin/api/tienda/cargar-stock", headers=H)

    assert r.status_code == 200
    assert r.json()["modelos"] == 2, "el 1 y el 3: el 2 está oculto y el 4 no tiene foto"
    assert hay(vacio, 1, "M") == 2 and hay(vacio, 3, "M") == 4
    assert vacio.filas([2]) == [] and vacio.filas([4]) == []


def test_las_filas_iniciales_dejan_afuera_lo_que_no_se_puede_ofrecer(tienda):
    sin_control = tienda.productos[1]
    sin_control["variants"][0]["stock"] = None  # la S no lleva cuenta en la tienda

    filas = admin.filas_iniciales(list(tienda.productos.values()))

    assert (1, "S", 0) not in filas
    assert (1, "M", 2) in filas
    assert not [f for f in filas if f[0] in (2, 4)]


# -- La página del panel -------------------------------------------------------


def test_la_pagina_del_panel_se_sirve_y_no_se_indexa():
    sys.modules.pop("sitio", None)
    import sitio as servidor

    c = TestClient(servidor.app)
    r = c.get("/admin/")

    assert r.status_code == 200
    assert "Panel mayorista" in r.text
    assert "noindex" in r.text
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-frame-options"] == "DENY"


def test_sin_configurar_el_panel_responde_503_y_no_deja_pasar():
    sys.modules.pop("sitio", None)
    import sitio as servidor

    c = TestClient(servidor.app)

    assert c.get("/admin/api/estado").json()["configurado"] is False
    assert c.get("/admin/api/stock").status_code == 503


# -- Ignorar modelos, por qué no entran y filtros --------------------------------


def ids(lista):
    return [x["id"] for x in lista]


def test_un_modelo_ignorado_deja_de_aparecer_como_nuevo(sesion, alm):
    r = sesion.post("/admin/api/tienda/ignorar", json={"producto": 3}, headers=H)

    assert r.status_code == 200
    d = sesion.get("/admin/api/tienda/nuevos").json()
    assert ids(d["nuevos"]) == []
    assert ids(d["ignorados"]) == [3]
    assert alm.filas([3]) == [], "ignorar no crea stock"


def test_un_modelo_ignorado_se_puede_volver_a_considerar(sesion):
    sesion.post("/admin/api/tienda/ignorar", json={"producto": 3}, headers=H)

    sesion.post("/admin/api/tienda/restaurar", json={"producto": 3}, headers=H)

    d = sesion.get("/admin/api/tienda/nuevos").json()
    assert ids(d["nuevos"]) == [3] and d["ignorados"] == []


def test_un_modelo_que_ya_esta_en_el_mayorista_no_se_ignora(sesion):
    assert sesion.post("/admin/api/tienda/ignorar", json={"producto": 1}, headers=H).status_code == 409


def test_incluir_un_modelo_ignorado_lo_saca_de_ignorados(sesion):
    sesion.post("/admin/api/tienda/ignorar", json={"producto": 3}, headers=H)

    assert sesion.post("/admin/api/tienda/incluir", json={"producto": 3}, headers=H).status_code == 200
    assert sesion.get("/admin/api/tienda/nuevos").json()["ignorados"] == []


def test_ignorar_y_volver_a_considerar_quedan_en_el_historial(sesion, memoria):
    sesion.get("/admin/api/tienda/nuevos")  # trae los nombres
    sesion.post("/admin/api/tienda/ignorar", json={"producto": 3}, headers=H)
    sesion.post("/admin/api/tienda/restaurar", json={"producto": 3}, headers=H)

    assert [(e["accion"], e["producto"]) for e in memoria.filas] == [("ignorado", "River Plate - Titular 26/27"), ("restaurado", "River Plate - Titular 26/27")]


def test_se_explica_por_que_cada_modelo_no_entra(sesion):
    d = sesion.get("/admin/api/tienda/nuevos").json()["no_entran"]

    assert ids(d["sin_foto"]) == [4]
    assert ids(d["ocultos"]) == [5], "el 2 también está oculto, pero ya está en el mayorista"
    assert ids(d["sin_control"]) == [6]
    assert d["sin_categoria"] == []


def test_los_modelos_sin_filtro_se_avisan(sesion):
    sesion.put("/admin/api/tienda/categorias", json={"categorias": [{"id": "boca", "nombre": "Boca", "tiendanube": ["BOCA"]}]}, headers=H)

    d = sesion.get("/admin/api/tienda/nuevos").json()["no_entran"]

    assert ids(d["sin_categoria"]) == [1, 3], "tienen stock o son nuevos y ninguna de sus categorías forma un filtro"


def test_la_lista_de_la_tienda_se_guarda_dos_minutos_salvo_que_se_pida_revisar(sesion, tienda):
    assert ids(sesion.get("/admin/api/tienda/nuevos").json()["nuevos"]) == [3]
    tienda.productos[7] = tienda._producto(7, "Otro nuevo", True, {"M": 2})

    assert ids(sesion.get("/admin/api/tienda/nuevos").json()["nuevos"]) == [3]
    assert ids(sesion.get("/admin/api/tienda/nuevos?refrescar=1").json()["nuevos"]) == [7, 3], "ordenados por nombre"


def test_los_filtros_arrancan_con_los_de_negocio_json(sesion):
    d = sesion.get("/admin/api/tienda/categorias").json()

    assert d["categorias"] == FILTROS
    assert {"nombre": "BOCA", "modelos": 0} in d["disponibles"]
    assert {"nombre": "FÚTBOL ARGENTINO", "modelos": 4} in d["disponibles"], "los publicados con esa categoría"


def test_los_filtros_se_guardan_y_la_web_los_relee(sesion, alm, invalidaciones, memoria):
    nuevos = [{"id": "boca", "nombre": " Boca  Juniors ", "tiendanube": ["BOCA", "BOCA", " "]}, {"id": "arg", "nombre": "Argentino", "tiendanube": ["FÚTBOL ARGENTINO"]}]

    r = sesion.put("/admin/api/tienda/categorias", json={"categorias": nuevos}, headers=H)

    assert r.status_code == 200 and r.json()["guardadas"] == 2
    guardadas = sesion.get("/admin/api/tienda/categorias").json()["categorias"]
    assert guardadas[0] == {"id": "boca", "nombre": "Boca Juniors", "tiendanube": ["BOCA"]}, "limpia espacios y repetidos"
    assert [c["id"] for c in guardadas] == ["boca", "arg"], "respeta el orden"
    assert invalidaciones
    assert memoria.filas[-1]["accion"] == "config"


def test_se_pueden_quitar_todos_los_filtros(sesion):
    sesion.put("/admin/api/tienda/categorias", json={"categorias": []}, headers=H)

    assert sesion.get("/admin/api/tienda/categorias").json()["categorias"] == []


@pytest.mark.parametrize(
    "categorias",
    [
        [{"id": "a", "nombre": "Uno", "tiendanube": []}, {"id": "b", "nombre": "uno", "tiendanube": []}],  # mismo nombre
        [{"id": "a", "nombre": "Uno", "tiendanube": []}, {"id": "a", "nombre": "Dos", "tiendanube": []}],  # mismo id
        [{"id": "A B", "nombre": "Uno", "tiendanube": []}],  # id con espacios y mayúsculas
        [{"id": "a", "nombre": "   ", "tiendanube": []}],  # sin nombre
        [{"id": "a", "nombre": "x" * 41, "tiendanube": []}],
    ],
)
def test_filtros_mal_armados_no_se_guardan(sesion, categorias):
    r = sesion.put("/admin/api/tienda/categorias", json={"categorias": categorias}, headers=H)

    assert r.status_code == 422
    assert sesion.get("/admin/api/tienda/categorias").json()["categorias"] == FILTROS


def test_los_ignorados_y_los_filtros_exigen_el_header_propio(sesion):
    assert sesion.post("/admin/api/tienda/ignorar", json={"producto": 3}).status_code == 403
    assert sesion.put("/admin/api/tienda/categorias", json={"categorias": []}).status_code == 403


# -- Los ajustes de la web --------------------------------------------------------


def web(**cambios):
    """Un conjunto completo de ajustes válidos, como lo manda el panel."""
    return {**{k: v for k, v in DEFECTO_WEB.items() if k != "logoHash"}, **cambios}


def test_los_ajustes_arrancan_con_los_de_siempre(sesion):
    d = sesion.get("/admin/api/web").json()

    assert d["ajustes"]["marca"] == "Mi Tienda" and d["ajustes"]["whatsapp"] == "5491100000000"
    assert d["ajustes"] == d["porDefecto"]
    assert d["logo"] is False


def test_guardar_los_ajustes_los_cambia_y_lo_anota(sesion, invalidaciones, memoria):
    r = sesion.put("/admin/api/web", json=web(whatsapp="5493562514675", marca="Simplemente Fútbol", minimo=12), headers=H)

    assert r.status_code == 200
    assert set(r.json()["cambios"]) == {"WhatsApp", "nombre", "pedido mínimo"}
    ver = sesion.get("/admin/api/web").json()["ajustes"]
    assert (ver["whatsapp"], ver["marca"], ver["minimo"]) == ("5493562514675", "Simplemente Fútbol", 12)
    assert invalidaciones
    assert memoria.filas[-1]["accion"] == "config" and "WhatsApp" in memoria.filas[-1]["talle"]


def test_guardar_sin_cambiar_nada_no_anota(sesion, memoria):
    sesion.put("/admin/api/web", json=web(), headers=H)

    assert memoria.filas == []


def test_los_ajustes_se_limpian_antes_de_guardar(sesion):
    sesion.put(
        "/admin/api/web",
        json=web(
            marca="  Mi   Tienda  ", color="#1FD65F",
            tramos=[{"desde": 12, "precio": 25000}, {"desde": 6, "precio": 28000}],
            reglas=[{"etiqueta": "Envío", "titulo": " Vía Cargo ", "texto": "A todo el país"}, {"etiqueta": "", "titulo": "  ", "texto": ""}],
        ),
        headers=H,
    )

    ver = sesion.get("/admin/api/web").json()["ajustes"]
    assert ver["marca"] == "Mi Tienda" and ver["color"] == "#1fd65f"
    assert [t["desde"] for t in ver["tramos"]] == [6, 12], "las escalas quedan de menor a mayor"
    assert ver["reglas"] == [{"etiqueta": "Envío", "titulo": "Vía Cargo", "texto": "A todo el país"}], "las reglas vacías se descartan"


@pytest.mark.parametrize(
    "cambios",
    [
        {"whatsapp": "+54 9 3562 514675"},
        {"whatsapp": "12345"},
        {"color": "verde"},
        {"color": "#12345"},
        {"marca": "   "},
        {"titulo": ""},
        {"minimo": 0},
        {"orden": "al azar"},
        {"mensaje": "Hola, quiero comprar"},  # sin {pedido}
        {"mensaje": "{pedido} {inventado}"},  # un dato que no existe
        {"tramos": [{"desde": 6, "precio": 1}, {"desde": 6, "precio": 2}]},
        {"instagram": "instagram.com/algo"},
        {"tiendaUrl": "javascript:alert(1)"},
        {"titulo": "x" * 41},
    ],
)
def test_ajustes_mal_armados_no_se_guardan(sesion, cambios):
    r = sesion.put("/admin/api/web", json=web(**cambios), headers=H)

    assert r.status_code == 422
    assert sesion.get("/admin/api/web").json()["ajustes"] == sesion.get("/admin/api/web").json()["porDefecto"]


LINEAS_WEB = [
    {"id": "top", "nombre": " Top   Quality ", "cats": ["arg"], "minimo": 6, "tramos": [{"desde": 12, "precio": 30000}, {"desde": 6, "precio": 35000}]},
    {"id": "g5", "nombre": "G5", "cats": [], "minimo": 6, "tramos": [{"desde": 6, "precio": 33500}]},
]


def test_el_panel_ofrece_las_categorias_para_armar_las_lineas(sesion):
    assert sesion.get("/admin/api/web").json()["categorias"] == [{"id": "arg", "nombre": "Fútbol argentino"}]


def test_las_lineas_de_precios_se_guardan_ordenadas_y_limpias(sesion, memoria):
    r = sesion.put("/admin/api/web", json=web(lineas=LINEAS_WEB), headers=H)

    assert r.status_code == 200 and r.json()["cambios"] == ["precios por línea"]
    top = sesion.get("/admin/api/web").json()["ajustes"]["lineas"][0]
    assert top["nombre"] == "Top Quality"
    assert [t["desde"] for t in top["tramos"]] == [6, 12], "las escalas quedan de menor a mayor"
    assert memoria.filas[-1]["accion"] == "config" and "precios por línea" in memoria.filas[-1]["talle"]


def test_guardar_otros_ajustes_no_pierde_las_lineas(sesion):
    sesion.put("/admin/api/web", json=web(lineas=LINEAS_WEB), headers=H)
    ver = sesion.get("/admin/api/web").json()["ajustes"]

    r = sesion.put("/admin/api/web", json=web(**{**ver, "marca": "Otra"}), headers=H)

    assert r.status_code == 200
    assert [l["id"] for l in sesion.get("/admin/api/web").json()["ajustes"]["lineas"]] == ["top", "g5"]


@pytest.mark.parametrize(
    "lineas",
    [
        [{**LINEAS_WEB[0]}, {**LINEAS_WEB[1], "id": "top"}],  # mismo identificador
        [{**LINEAS_WEB[0]}, {**LINEAS_WEB[1], "cats": ["arg"]}],  # una categoría en dos líneas
        [{**LINEAS_WEB[0], "tramos": [{"desde": 6, "precio": 1}, {"desde": 6, "precio": 2}]}],  # dos escalas desde lo mismo
        [{**LINEAS_WEB[0], "minimo": 0}],
        [{**LINEAS_WEB[0], "nombre": ""}],
        [{**LINEAS_WEB[0], "id": "Con Espacios"}],
    ],
)
def test_lineas_mal_armadas_no_se_guardan(sesion, lineas):
    r = sesion.put("/admin/api/web", json=web(lineas=lineas), headers=H)

    assert r.status_code == 422
    assert sesion.get("/admin/api/web").json()["ajustes"]["lineas"] == []


def test_el_mensaje_puede_usar_todos_los_datos_conocidos(sesion):
    todos = " ".join("{" + d + "}" for d in ajustes.DATOS_DEL_MENSAJE)

    assert sesion.put("/admin/api/web", json=web(mensaje=todos), headers=H).status_code == 200


def test_el_logo_se_sube_se_ve_y_se_puede_quitar(sesion, alm):
    r = sesion.put("/admin/api/web/logo", json={"datos": "data:image/png;base64," + PNG_LOGO}, headers=H)

    assert r.status_code == 200
    guardado = alm.config("logo")
    assert guardado["tipo"] == "image/png" and guardado["hash"] == r.json()["version"]
    d = sesion.get("/admin/api/web").json()
    assert d["logo"] is True and d["ajustes"]["logoHash"] == r.json()["version"]

    sesion.delete("/admin/api/web/logo", headers=H)
    d = sesion.get("/admin/api/web").json()
    assert d["logo"] is False and d["ajustes"]["logoHash"] == ""


def test_guardar_los_ajustes_no_pierde_el_logo(sesion):
    version = sesion.put("/admin/api/web/logo", json={"datos": PNG_LOGO}, headers=H).json()["version"]

    sesion.put("/admin/api/web", json=web(marca="Otra"), headers=H)

    assert sesion.get("/admin/api/web").json()["ajustes"]["logoHash"] == version


@pytest.mark.parametrize(
    "datos",
    [
        pytest.param("esto no es una imagen", id="no-es-base64"),
        pytest.param(base64.b64encode(b"texto cualquiera").decode(), id="no-es-imagen"),
        pytest.param(base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * (1024 * 1024)).decode(), id="pesa-mas-de-1mb"),
    ],
)
def test_un_logo_que_no_sirve_no_se_acepta(sesion, alm, datos):
    r = sesion.put("/admin/api/web/logo", json={"datos": datos}, headers=H)

    assert r.status_code == 422
    assert alm.config("logo") is None


def test_los_ajustes_exigen_el_header_propio(sesion):
    assert sesion.put("/admin/api/web", json=web()).status_code == 403
    assert sesion.put("/admin/api/web/logo", json={"datos": PNG_LOGO}).status_code == 403
    assert sesion.delete("/admin/api/web/logo").status_code == 403


# -- El agente de WhatsApp -------------------------------------------------------------

PROMPT = "Sos el asistente de la tienda. Atendés por WhatsApp en español rioplatense."


def test_sin_versiones_publicadas_el_panel_muestra_el_prompt_del_repositorio(sesion):
    d = sesion.get("/admin/api/agente").json()

    assert d["agente"] == {"conectado": True, "error": "", "modelo": "gpt-5", "proveedor": "openai"}
    assert d["sinPublicar"] is True and d["prompt"]["id"] is None and d["prompt"]["texto"].startswith("Sos el asistente")
    assert d["versiones"] == []
    assert d["ajustes"]["mensajesPorRespuesta"] == 1 and d["ajustes"]["avisosEmail"] == "duenio@ejemplo.com", "los del entorno, mientras no se guarde nada"
    assert d["datos"] == {v: "" for v in admin.VARIABLES_DEL_AGENTE} and d["variables"] == list(admin.VARIABLES_DEL_AGENTE)


def test_si_el_agente_no_contesta_el_panel_lo_dice_y_sigue_mostrando_lo_guardado(sesion, agente, alm):
    alm.publicar_prompt(PROMPT, "leo", "primera")
    agente.caido = True

    d = sesion.get("/admin/api/agente").json()

    assert d["agente"]["conectado"] is False and "connection refused" in d["agente"]["error"]
    assert d["prompt"]["texto"] == PROMPT and d["sinPublicar"] is False


def test_sin_agente_conectado_se_avisa(monkeypatch, tienda, memoria, alm):
    monkeypatch.setattr(acceso, "USUARIO", "leo")
    monkeypatch.setattr(acceso, "CLAVE", CLAVE)
    monkeypatch.setattr(acceso, "SECRETO", "s" * 40)
    acceso._fallos.clear()
    app = FastAPI()
    app.include_router(admin.crear_router(memoria, lambda: None, alm, FILTROS, lambda: dict(DEFECTO_WEB), None))
    c = TestClient(app)
    c.post("/admin/api/login", json={"usuario": "leo", "clave": CLAVE}, headers=H)

    d = c.get("/admin/api/agente").json()

    assert d["agente"]["conectado"] is False and "AGENTE_PANEL_TOKEN" in d["agente"]["error"]
    assert c.post("/admin/api/agente/probar", json={"prompt": PROMPT, "mensajes": [{"rol": "yo", "texto": "hola"}]}, headers=H).status_code == 503


def test_publicar_el_prompt_lo_deja_en_uso_y_en_el_historial(sesion, alm, memoria):
    r = sesion.put("/admin/api/agente/prompt", json={"texto": PROMPT, "nota": "Primer cambio"}, headers=H)

    assert r.status_code == 200 and r.json()["id"] == 1
    d = sesion.get("/admin/api/agente").json()
    assert d["sinPublicar"] is False and d["prompt"]["texto"] == PROMPT and d["prompt"]["nota"] == "Primer cambio" and d["prompt"]["usuario"] == "leo"
    assert [(v["id"], v["activa"]) for v in d["versiones"]] == [(1, True)]
    assert "texto" not in d["versiones"][0], "la lista no carga los textos enteros"
    assert memoria.filas[-1]["accion"] == "config" and "versión 1" in memoria.filas[-1]["talle"]


def test_cada_publicacion_es_una_version_nueva_y_solo_la_ultima_esta_en_uso(sesion, alm):
    sesion.put("/admin/api/agente/prompt", json={"texto": PROMPT}, headers=H)
    sesion.put("/admin/api/agente/prompt", json={"texto": PROMPT + " Sé breve."}, headers=H)

    d = sesion.get("/admin/api/agente").json()

    assert [(v["id"], v["activa"]) for v in d["versiones"]] == [(2, True), (1, False)]
    assert alm.prompt_activo()["id"] == 2


def test_publicar_un_prompt_igual_al_que_esta_en_uso_no_hace_nada(sesion):
    sesion.put("/admin/api/agente/prompt", json={"texto": PROMPT}, headers=H)

    r = sesion.put("/admin/api/agente/prompt", json={"texto": "  " + PROMPT + "\n"}, headers=H)

    assert r.status_code == 422 and len(sesion.get("/admin/api/agente").json()["versiones"]) == 1


@pytest.mark.parametrize("texto", ["corto", "x" * 30001, PROMPT + " {{inventada}}"])
def test_prompts_que_no_se_pueden_publicar(sesion, alm, texto):
    r = sesion.put("/admin/api/agente/prompt", json={"texto": texto}, headers=H)

    assert r.status_code == 422 and alm.prompt_activo() is None


def test_se_avisa_que_variables_del_prompt_no_tienen_dato(sesion):
    r = sesion.put("/admin/api/agente/prompt", json={"texto": PROMPT + " Alias: {{alias_pago}}. Horario: {{horario}}."}, headers=H)
    assert r.json()["vacias"] == ["alias_pago", "horario"]

    sesion.put("/admin/api/agente/datos", json={"alias_pago": "sf.pagos"}, headers=H)
    r = sesion.put("/admin/api/agente/prompt", json={"texto": PROMPT + " Alias: {{alias_pago}}. Horario: {{horario}}. Otra vez."}, headers=H)

    assert r.json()["vacias"] == ["horario"]


def test_restaurar_una_version_la_publica_como_una_nueva(sesion, alm):
    sesion.put("/admin/api/agente/prompt", json={"texto": PROMPT}, headers=H)
    sesion.put("/admin/api/agente/prompt", json={"texto": PROMPT + " Sé breve."}, headers=H)

    r = sesion.post("/admin/api/agente/prompt/restaurar", json={"id": 1}, headers=H)

    assert r.status_code == 200 and r.json()["id"] == 3
    activo = alm.prompt_activo()
    assert activo["texto"] == PROMPT and activo["nota"] == "Restaurada de la versión 1"
    assert len(alm.prompt_versiones()) == 3, "el historial no se pierde"


def test_restaurar_una_version_que_no_existe_da_404(sesion):
    assert sesion.post("/admin/api/agente/prompt/restaurar", json={"id": 99}, headers=H).status_code == 404
    assert sesion.get("/admin/api/agente/versiones/99").status_code == 404


def test_se_puede_ver_el_texto_de_una_version_vieja(sesion):
    sesion.put("/admin/api/agente/prompt", json={"texto": PROMPT}, headers=H)
    sesion.put("/admin/api/agente/prompt", json={"texto": PROMPT + " Sé breve."}, headers=H)

    assert sesion.get("/admin/api/agente/versiones/1").json()["texto"] == PROMPT


AJUSTES_AGENTE = {"activo": True, "mensajesPorRespuesta": 2, "topeMensajesPorDia": 20, "respetarResueltas": True, "soloElPrimerMensaje": False, "avisosEmail": "otro@ejemplo.com"}


def test_los_ajustes_del_agente_se_guardan_y_se_ven(sesion, alm, memoria):
    r = sesion.put("/admin/api/agente/ajustes", json=AJUSTES_AGENTE, headers=H)

    assert r.status_code == 200 and set(r.json()["cambios"]) == {"mensajes por respuesta", "tope por día", "mail de avisos"}
    assert alm.config("agente_ajustes")["mensajesPorRespuesta"] == 2
    d = sesion.get("/admin/api/agente").json()["ajustes"]
    assert (d["mensajesPorRespuesta"], d["topeMensajesPorDia"], d["avisosEmail"]) == (2, 20, "otro@ejemplo.com")
    assert memoria.filas[-1]["producto"] == "Ajustes del agente"


def test_pausar_el_agente_queda_anotado(sesion, memoria):
    sesion.put("/admin/api/agente/ajustes", json={**AJUSTES_AGENTE, "activo": False}, headers=H)

    assert sesion.get("/admin/api/agente").json()["ajustes"]["activo"] is False
    assert "agente activo" in memoria.filas[-1]["talle"]


@pytest.mark.parametrize("cambios", [{"mensajesPorRespuesta": 0}, {"mensajesPorRespuesta": 6}, {"topeMensajesPorDia": -1}, {"topeMensajesPorDia": 1001}, {"avisosEmail": "no-es-un-mail"}])
def test_ajustes_del_agente_que_no_sirven(sesion, alm, cambios):
    r = sesion.put("/admin/api/agente/ajustes", json={**AJUSTES_AGENTE, **cambios}, headers=H)

    assert r.status_code == 422 and alm.config("agente_ajustes") is None


def test_sin_mail_de_avisos_es_valido(sesion):
    assert sesion.put("/admin/api/agente/ajustes", json={**AJUSTES_AGENTE, "avisosEmail": ""}, headers=H).status_code == 200


def test_los_datos_del_negocio_se_guardan_limpios(sesion, alm, memoria):
    r = sesion.put("/admin/api/agente/datos", json={"alias_pago": "  sf.pagos  ", "horario": "de   9 a 18"}, headers=H)

    assert r.status_code == 200 and set(r.json()["cambios"]) == {"alias_pago", "horario"}
    assert alm.config("agente_datos")["alias_pago"] == "sf.pagos" and alm.config("agente_datos")["horario"] == "de 9 a 18"
    assert sesion.get("/admin/api/agente").json()["datos"]["costo_envio"] == ""


def test_probar_le_manda_al_agente_el_prompt_del_editor_y_los_datos(sesion, agente):
    sesion.put("/admin/api/agente/datos", json={"alias_pago": "sf.pagos"}, headers=H)

    r = sesion.post("/admin/api/agente/probar", json={"prompt": PROMPT + " {{alias_pago}}", "mensajes": [{"rol": "yo", "texto": "hola"}]}, headers=H)

    assert r.status_code == 200 and r.json()["texto"] == "Hola, ¿qué talle buscás?"
    prompt, mensajes, datos = agente.pruebas[0]
    assert prompt.endswith("{{alias_pago}}"), "el agente es quien reemplaza las variables"
    assert mensajes == [{"rol": "yo", "texto": "hola"}] and datos["alias_pago"] == "sf.pagos"


def test_la_prueba_tiene_que_terminar_con_un_mensaje_de_la_persona(sesion, agente):
    r = sesion.post("/admin/api/agente/probar", json={"prompt": PROMPT, "mensajes": [{"rol": "yo", "texto": "hola"}, {"rol": "agente", "texto": "buenas"}]}, headers=H)

    assert r.status_code == 422 and agente.pruebas == []


def test_si_el_agente_falla_la_prueba_lo_dice(sesion, agente):
    agente.caido = True

    r = sesion.post("/admin/api/agente/probar", json={"prompt": PROMPT, "mensajes": [{"rol": "yo", "texto": "hola"}]}, headers=H)

    assert r.status_code == 502 and "El modelo no contestó" in r.json()["detail"]


def test_los_cambios_del_agente_exigen_el_header_propio(sesion):
    assert sesion.put("/admin/api/agente/prompt", json={"texto": PROMPT}).status_code == 403
    assert sesion.put("/admin/api/agente/ajustes", json=AJUSTES_AGENTE).status_code == 403
    assert sesion.put("/admin/api/agente/datos", json={}).status_code == 403
    assert sesion.post("/admin/api/agente/probar", json={"prompt": PROMPT, "mensajes": [{"rol": "yo", "texto": "x"}]}).status_code == 403


# -- Personas con acceso y permisos ---------------------------------------------------------

CLAVE_PABLO = "clave-de-pablo-123"


def persona(sesion, usuario="pablo", rol="stock", clave=CLAVE_PABLO):
    r = sesion.post("/admin/api/usuarios", json={"usuario": usuario, "clave": clave, "rol": rol}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def entrar_como(cliente, usuario="pablo", clave=CLAVE_PABLO):
    """Otra persona entrando al mismo panel (su propio navegador)."""
    otro = TestClient(cliente.app)
    r = otro.post("/admin/api/login", json={"usuario": usuario, "clave": clave}, headers=H)
    assert r.status_code == 200, r.text
    return otro


def test_el_duenio_da_de_alta_a_una_persona_y_ella_entra(sesion, cliente):
    p = persona(sesion, rol="stock")

    assert p["usuario"] == "pablo" and p["rol"] == "stock" and p["activo"] is True and "clave" not in str(p)
    otro = entrar_como(cliente)
    assert otro.get("/admin/api/estado").json()["permisos"] == ["stock", "cuenta"]


def test_la_clave_de_una_persona_no_se_guarda_en_claro(sesion, alm):
    persona(sesion)

    guardada = alm.usuario("pablo")["clave_hash"]

    assert CLAVE_PABLO not in guardada and guardada.startswith("scrypt$")


@pytest.mark.parametrize("usuario, clave", [("pablo", "mala-mala-mala"), ("PABLOX", CLAVE_PABLO), ("", ""), ("leo", CLAVE_PABLO)])
def test_con_datos_equivocados_la_persona_no_entra(sesion, cliente, usuario, clave):
    persona(sesion)

    r = TestClient(cliente.app).post("/admin/api/login", json={"usuario": usuario, "clave": clave}, headers=H)

    assert r.status_code == 401


def test_el_usuario_se_escribe_sin_importar_mayusculas(sesion, cliente):
    persona(sesion, usuario="Pablo")

    assert entrar_como(cliente, "PABLO").get("/admin/api/estado").json()["usuario"] == "pablo"


@pytest.mark.parametrize(
    "rol, permitido, prohibido",
    [
        ("stock", ["/admin/api/stock", "/admin/api/pedidos", "/admin/api/historial", "/admin/api/tienda/nuevos", "/admin/api/cuenta"], ["/admin/api/web", "/admin/api/tienda/categorias", "/admin/api/agente", "/admin/api/avisos"]),
        ("stock_web", ["/admin/api/stock", "/admin/api/web", "/admin/api/tienda/categorias", "/admin/api/cuenta"], ["/admin/api/agente", "/admin/api/avisos"]),
        ("todo", ["/admin/api/stock", "/admin/api/web", "/admin/api/agente", "/admin/api/cuenta"], ["/admin/api/avisos"]),
    ],
)
def test_cada_rol_entra_solo_a_lo_suyo(sesion, cliente, rol, permitido, prohibido):
    persona(sesion, rol=rol)
    otro = entrar_como(cliente)

    assert [otro.get(ruta).status_code for ruta in permitido] == [200] * len(permitido)
    assert [otro.get(ruta).status_code for ruta in prohibido] == [403] * len(prohibido)


def test_ninguna_persona_puede_administrar_a_las_demas(sesion, cliente):
    persona(sesion, rol="todo")
    otro = entrar_como(cliente)

    assert otro.post("/admin/api/usuarios", json={"usuario": "otra", "clave": CLAVE_PABLO, "rol": "stock"}, headers=H).status_code == 403
    assert otro.put("/admin/api/usuarios/pablo", json={"rol": "stock"}, headers=H).status_code == 403
    assert otro.put("/admin/api/avisos", json={}, headers=H).status_code == 403


def test_un_cambio_de_rol_rige_en_la_sesion_que_ya_estaba_abierta(sesion, cliente):
    persona(sesion, rol="todo")
    otro = entrar_como(cliente)
    assert otro.get("/admin/api/agente").status_code == 200

    sesion.put("/admin/api/usuarios/pablo", json={"rol": "stock"}, headers=H)

    assert otro.get("/admin/api/agente").status_code == 403


def test_desactivar_a_una_persona_la_saca_al_instante(sesion, cliente):
    persona(sesion)
    otro = entrar_como(cliente)

    sesion.put("/admin/api/usuarios/pablo", json={"activo": False}, headers=H)

    assert otro.get("/admin/api/stock").status_code == 401
    assert TestClient(cliente.app).post("/admin/api/login", json={"usuario": "pablo", "clave": CLAVE_PABLO}, headers=H).status_code == 401
    sesion.put("/admin/api/usuarios/pablo", json={"activo": True}, headers=H)
    assert entrar_como(cliente).get("/admin/api/stock").status_code == 200


def test_quitar_a_una_persona_la_saca_al_instante(sesion, cliente):
    persona(sesion)
    otro = entrar_como(cliente)

    assert sesion.delete("/admin/api/usuarios/pablo", headers=H).status_code == 200

    assert otro.get("/admin/api/stock").status_code == 401


def test_el_duenio_resetea_la_clave_de_una_persona(sesion, cliente):
    persona(sesion)

    sesion.put("/admin/api/usuarios/pablo", json={"clave": "otra-clave-nueva-99"}, headers=H)

    assert TestClient(cliente.app).post("/admin/api/login", json={"usuario": "pablo", "clave": CLAVE_PABLO}, headers=H).status_code == 401
    assert entrar_como(cliente, clave="otra-clave-nueva-99").get("/admin/api/stock").status_code == 200


@pytest.mark.parametrize(
    "cuerpo, estado",
    [
        ({"usuario": "leo", "clave": CLAVE_PABLO, "rol": "stock"}, 422),  # el usuario del dueño
        ({"usuario": "LEO", "clave": CLAVE_PABLO, "rol": "stock"}, 422),
        ({"usuario": "pablo", "clave": "corta", "rol": "stock"}, 422),
        ({"usuario": "pablo", "clave": "pablo-pablo", "rol": "stock"}, 200),
        ({"usuario": "pablo", "clave": "pablo", "rol": "stock"}, 422),
        ({"usuario": "pa blo", "clave": CLAVE_PABLO, "rol": "stock"}, 422),
        ({"usuario": "ab", "clave": CLAVE_PABLO, "rol": "stock"}, 422),
        ({"usuario": "pablo", "clave": CLAVE_PABLO, "rol": "duenio"}, 422),
        ({"usuario": "pablo", "clave": CLAVE_PABLO, "rol": "inventado"}, 422),
    ],
)
def test_altas_que_no_se_aceptan(sesion, cuerpo, estado):
    assert sesion.post("/admin/api/usuarios", json=cuerpo, headers=H).status_code == estado


def test_no_se_repite_un_usuario(sesion):
    persona(sesion)

    assert sesion.post("/admin/api/usuarios", json={"usuario": "PABLO", "clave": CLAVE_PABLO, "rol": "stock"}, headers=H).status_code == 409


def test_el_duenio_no_se_toca_desde_la_lista(sesion):
    assert sesion.put("/admin/api/usuarios/leo", json={"activo": False}, headers=H).status_code == 422
    assert sesion.delete("/admin/api/usuarios/leo", headers=H).status_code == 422
    assert sesion.put("/admin/api/usuarios/nadie", json={"activo": False}, headers=H).status_code == 404


def test_la_cuenta_del_duenio_muestra_personas_roles_y_conexiones(sesion):
    persona(sesion, "pablo", "stock")
    persona(sesion, "lucia", "todo")

    d = sesion.get("/admin/api/cuenta").json()

    assert d["esDuenio"] is True and d["rol"] == "duenio"
    assert [p["usuario"] for p in d["personas"]] == ["pablo", "lucia"] and "clave" not in str(d["personas"])
    assert [r["id"] for r in d["roles"]] == ["todo", "stock_web", "stock"]
    estados = {c["id"]: c["estado"] for c in d["conexiones"]}
    assert estados == {"tiendanube": "ok", "agente": "ok", "chatwoot": "ok", "whatsapp": "pendiente", "correo": "ok"}
    assert d["avisosPara"] == "duenio@ejemplo.com"


def test_la_cuenta_de_una_persona_no_muestra_a_las_demas_ni_el_mail(sesion, cliente):
    persona(sesion)

    d = entrar_como(cliente).get("/admin/api/cuenta").json()

    assert d["esDuenio"] is False and "personas" not in d and "avisosPara" not in d and "duenio@ejemplo.com" not in str(d)


def test_si_el_agente_no_contesta_la_cuenta_lo_dice(sesion, agente):
    agente.caido = True

    estados = {c["id"]: c["estado"] for c in sesion.get("/admin/api/cuenta").json()["conexiones"]}

    assert estados["agente"] == "error" and estados["chatwoot"] == "error"


def test_una_persona_cambia_su_clave(sesion, cliente):
    persona(sesion)
    otro = entrar_como(cliente)

    r = otro.post("/admin/api/cuenta/clave", json={"actual": CLAVE_PABLO, "nueva": "clave-nueva-de-pablo"}, headers=H)

    assert r.status_code == 200
    assert TestClient(cliente.app).post("/admin/api/login", json={"usuario": "pablo", "clave": CLAVE_PABLO}, headers=H).status_code == 401
    assert entrar_como(cliente, clave="clave-nueva-de-pablo").get("/admin/api/stock").status_code == 200


@pytest.mark.parametrize(
    "actual, nueva",
    [("equivocada-123", "clave-nueva-de-pablo"), (CLAVE_PABLO, "corta"), (CLAVE_PABLO, CLAVE_PABLO), (CLAVE_PABLO, "pablo"), (CLAVE_PABLO, "x" * 201)],
)
def test_cambios_de_clave_que_no_se_aceptan(sesion, cliente, actual, nueva):
    persona(sesion)
    r = entrar_como(cliente).post("/admin/api/cuenta/clave", json={"actual": actual, "nueva": nueva}, headers=H)

    assert r.status_code == 422


def test_el_duenio_cambia_su_clave_y_la_de_arranque_deja_de_valer(sesion, cliente):
    r = sesion.post("/admin/api/cuenta/clave", json={"actual": CLAVE, "nueva": "la-clave-nueva-del-duenio"}, headers=H)

    assert r.status_code == 200
    nuevo = TestClient(cliente.app)
    assert nuevo.post("/admin/api/login", json={"usuario": "leo", "clave": CLAVE}, headers=H).status_code == 401
    assert nuevo.post("/admin/api/login", json={"usuario": "leo", "clave": "la-clave-nueva-del-duenio"}, headers=H).status_code == 200


def test_con_restablecer_vuelve_a_valer_la_clave_de_arranque(sesion, cliente, monkeypatch):
    sesion.post("/admin/api/cuenta/clave", json={"actual": CLAVE, "nueva": "la-clave-nueva-del-duenio"}, headers=H)
    monkeypatch.setattr(acceso, "RESTABLECER", True)

    assert TestClient(cliente.app).post("/admin/api/login", json={"usuario": "leo", "clave": CLAVE}, headers=H).status_code == 200


def test_el_ingreso_queda_anotado(sesion, cliente, alm):
    persona(sesion)

    entrar_como(cliente)

    assert alm.usuario("pablo")["ultimo_ingreso"] is not None
    assert alm.usuario("leo")["ultimo_ingreso"] is not None


def test_las_altas_y_bajas_quedan_en_el_historial(sesion, memoria):
    persona(sesion)
    sesion.put("/admin/api/usuarios/pablo", json={"rol": "todo", "activo": False}, headers=H)
    sesion.delete("/admin/api/usuarios/pablo", headers=H)

    detalle = [e["talle"] for e in memoria.filas if e["producto"] == "Personas con acceso"]

    assert detalle[0].startswith("alta de pablo") and "desactivada" in detalle[1] and detalle[2] == "baja de pablo"
    assert CLAVE_PABLO not in str(memoria.filas)


# -- Avisos por mail --------------------------------------------------------------------------


def test_los_avisos_arrancan_prendidos_con_umbral_de_dos(sesion):
    assert sesion.get("/admin/api/avisos").json() == {"pedidoNuevo": True, "pocasUnidades": True, "umbral": 2, "modelosNuevos": True, "fallasDeLaTienda": True}


def test_los_avisos_se_configuran(sesion, memoria):
    r = sesion.put("/admin/api/avisos", json={"pedidoNuevo": False, "pocasUnidades": True, "umbral": 5, "modelosNuevos": True, "fallasDeLaTienda": True}, headers=H)

    assert r.status_code == 200 and set(r.json()["cambios"]) == {"pedido nuevo", "umbral"}
    assert sesion.get("/admin/api/avisos").json()["umbral"] == 5
    assert memoria.filas[-1]["producto"] == "Avisos por mail"


@pytest.mark.parametrize("cambios", [{"umbral": -1}, {"umbral": 101}])
def test_avisos_que_no_se_aceptan(sesion, cambios):
    assert sesion.put("/admin/api/avisos", json=cambios, headers=H).status_code == 422


def test_el_mail_de_prueba_lo_manda_el_agente(sesion):
    assert sesion.post("/admin/api/avisos/probar", headers=H).json() == {"ok": True, "para": "duenio@ejemplo.com"}


def test_al_bajar_un_modelo_al_umbral_se_avisa_una_vez(sesion, alm, agente):
    sesion.get("/admin/api/stock")  # la planilla trae los nombres
    sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(alm, 1, "M", 1), cambio(alm, 1, "L", 0)]}, headers=H)  # el modelo 1 tenía 5: queda en 1
    asunto, cuerpo, tipo = agente.avisos[0]

    assert len(agente.avisos) == 1 and "pocas unidades" in asunto.lower() and "Boca Juniors" in cuerpo and "1 u." in cuerpo


def test_si_ya_estaba_bajo_el_umbral_no_se_vuelve_a_avisar(sesion, alm, agente):
    sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(alm, 1, "M", 0), cambio(alm, 1, "L", 1)]}, headers=H)
    avisos_antes = len(agente.avisos)

    sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(alm, 1, "L", 0)]}, headers=H)

    assert avisos_antes == 1 and len(agente.avisos) == 1


def test_un_cambio_que_no_baja_al_umbral_no_avisa(sesion, alm, agente):
    sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(alm, 1, "M", 4)]}, headers=H)

    assert agente.avisos == []


def test_confirmar_un_pedido_que_deja_pocas_unidades_avisa(sesion, alm, agente):
    pedido(alm, "A-AAAAA", (1, "M", 2), (1, "L", 3))  # el modelo tiene 5: queda en 0

    sesion.post("/admin/api/pedidos/A-AAAAA/confirmar", headers=H)

    assert len(agente.avisos) == 1 and "agotado" in agente.avisos[0][1]


def test_con_el_aviso_apagado_no_sale_nada(sesion, alm, agente):
    sesion.put("/admin/api/avisos", json={"pedidoNuevo": True, "pocasUnidades": False, "umbral": 2, "modelosNuevos": True, "fallasDeLaTienda": True}, headers=H)

    sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(alm, 1, "M", 0), cambio(alm, 1, "L", 0)]}, headers=H)

    assert agente.avisos == []


def test_el_umbral_se_puede_subir(sesion, alm, agente):
    sesion.put("/admin/api/avisos", json={"pedidoNuevo": True, "pocasUnidades": True, "umbral": 4, "modelosNuevos": True, "fallasDeLaTienda": True}, headers=H)

    sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(alm, 1, "M", 2), cambio(alm, 1, "L", 2)]}, headers=H)

    assert len(agente.avisos) == 1 and "4 unidades o menos" in agente.avisos[0][1]


def test_un_aviso_que_falla_no_rompe_el_guardado(sesion, alm, agente, monkeypatch):
    def cae(*a):
        raise agente_cliente.ErrorDelAgente("sin mail")

    monkeypatch.setattr(agente, "avisar", cae)

    r = sesion.post("/admin/api/stock/aplicar", json={"cambios": [cambio(alm, 1, "M", 0), cambio(alm, 1, "L", 0)]}, headers=H)

    assert r.status_code == 200 and fila(alm, 1, "M")["cantidad"] == 0
