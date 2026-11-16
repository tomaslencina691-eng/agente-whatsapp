"""Pruebas de las claves y los roles (acceso.py) y de los avisos por mail (avisos.py). No usan base."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mayorista"))

import acceso  # noqa: E402
import almacen  # noqa: E402
import avisos  # noqa: E402


def test_una_clave_se_guarda_con_sal_y_se_verifica():
    h1, h2 = acceso.hashear("mi-clave-larga-123"), acceso.hashear("mi-clave-larga-123")

    assert h1 != h2, "cada clave lleva su propia sal"
    assert acceso.verificar("mi-clave-larga-123", h1) and not acceso.verificar("otra", h1)
    assert "mi-clave" not in h1


@pytest.mark.parametrize("guardada", ["", "basura", "scrypt$no$base64", "md5$aa$bb", "scrypt$%%%$%%%"])
def test_una_clave_guardada_rota_no_deja_pasar_a_nadie(guardada):
    assert acceso.verificar("lo-que-sea-123", guardada) is False


@pytest.mark.parametrize("clave, usuario, hay_problema", [("corta", "", True), ("x" * 201, "", True), ("pablo-pablo", "pablo", False), ("PABLO", "pablo", True), ("una-clave-bien-larga", "pablo", False)])
def test_que_claves_sirven(clave, usuario, hay_problema):
    assert bool(acceso.problema_de_clave(clave, usuario)) is hay_problema


@pytest.mark.parametrize(
    "ruta, grupo",
    [
        ("/admin/api/stock", "stock"), ("/admin/api/stock/aplicar", "stock"), ("/admin/api/foto/3", "stock"), ("/admin/api/historial/abc/deshacer", "stock"),
        ("/admin/api/pedidos/A-AAAAA/confirmar", "stock"), ("/admin/api/tienda/nuevos", "stock"), ("/admin/api/tienda/incluir", "stock"),
        ("/admin/api/tienda/categorias", "web"), ("/admin/api/web", "web"), ("/admin/api/web/logo", "web"),
        ("/admin/api/agente", "agente"), ("/admin/api/agente/probar", "agente"),
        ("/admin/api/cuenta", "cuenta"), ("/admin/api/cuenta/clave", "cuenta"),
        ("/admin/api/usuarios", "administrar"), ("/admin/api/usuarios/pablo", "administrar"), ("/admin/api/avisos", "administrar"), ("/admin/api/avisos/probar", "administrar"),
    ],
)
def test_cada_parte_del_panel_pide_su_permiso(ruta, grupo):
    assert acceso.grupo_de(ruta) == grupo


def test_los_roles_no_se_pasan_de_lo_que_deben():
    assert "administrar" not in acceso.ROLES["todo"] and "agente" not in acceso.ROLES["stock_web"] and "web" not in acceso.ROLES["stock"]
    assert set(acceso.ROLES["duenio"]) == {"stock", "web", "agente", "cuenta", "administrar"}
    assert "duenio" not in acceso.ROLES_ASIGNABLES, "el rol de dueño no se le da a nadie desde el panel"


def test_una_sesion_de_una_persona_que_ya_no_existe_no_vale(monkeypatch):
    monkeypatch.setattr(acceso, "USUARIO", "leo")

    assert acceso.identidad(almacen.AlmacenMemoria(), "fantasma") is None
    assert acceso.identidad(almacen.AlmacenMemoria(), None) is None
    assert acceso.identidad(None, "leo")["rol"] == "duenio"


# -- avisos.py ------------------------------------------------------------------------------------


def filas(**totales):
    return [{"id": i, "producto_id": int(p[1:]), "talle": "M", "cantidad": n} for i, (p, n) in enumerate(totales.items())]


def test_se_avisa_al_cruzar_el_umbral_y_no_mientras_sigue_abajo():
    assert avisos.cruzaron_el_umbral(filas(m1=5), filas(m1=2), 2) == [(1, 2)]
    assert avisos.cruzaron_el_umbral(filas(m1=5), filas(m1=0), 2) == [(1, 0)]
    assert avisos.cruzaron_el_umbral(filas(m1=2), filas(m1=1), 2) == []
    assert avisos.cruzaron_el_umbral(filas(m1=5), filas(m1=3), 2) == []
    assert avisos.cruzaron_el_umbral(filas(m1=1), filas(m1=9), 2) == []


def test_se_suman_los_talles_de_cada_modelo():
    antes = [{"id": 1, "producto_id": 1, "talle": "M", "cantidad": 3}, {"id": 2, "producto_id": 1, "talle": "L", "cantidad": 3}]
    despues = [{"id": 1, "producto_id": 1, "talle": "M", "cantidad": 1}, {"id": 2, "producto_id": 1, "talle": "L", "cantidad": 1}]

    assert avisos.cruzaron_el_umbral(antes, despues, 2) == [(1, 2)]


class AgenteDeMentira:
    def __init__(self):
        self.avisos = []

    def avisar(self, asunto, cuerpo, tipo):
        self.avisos.append((asunto, cuerpo, tipo))


def test_sin_agente_los_avisos_no_hacen_nada():
    avisos.Avisador(almacen.AlmacenMemoria(), None, sincrono=True).pedido_nuevo("A-AAAAA", 6, None, "", "")


def test_la_configuracion_de_avisos_se_lee_cada_quince_segundos():
    hora = [0.0]
    alm = almacen.AlmacenMemoria()
    a = avisos.Avisador(alm, AgenteDeMentira(), sincrono=True, reloj=lambda: hora[0])
    assert a.config()["umbral"] == 2
    alm.guardar_config("avisos", {"umbral": 9, "campoViejo": 1})

    assert a.config()["umbral"] == 2
    hora[0] = 16
    assert a.config()["umbral"] == 9 and "campoViejo" not in a.config()


def test_si_la_base_falla_se_siguen_usando_los_ultimos_ajustes_o_los_de_siempre():
    class Rota(almacen.AlmacenMemoria):
        def config(self, clave):
            raise OSError("sin base")

    assert avisos.Avisador(Rota(), AgenteDeMentira(), sincrono=True).config() == avisos.DEFECTO


def test_un_pedido_sin_nombre_dice_que_no_dejo_su_nombre():
    agente = AgenteDeMentira()

    avisos.Avisador(almacen.AlmacenMemoria(), agente, sincrono=True).pedido_nuevo("A-AAAAA", 1, None, "", "")

    assert "1 camiseta" in agente.avisos[0][0] and "no dejó su nombre" in agente.avisos[0][1] and "Total estimado" not in agente.avisos[0][1]


def test_hasta_treinta_modelos_nuevos_en_la_lista_y_el_resto_se_cuenta():
    agente = AgenteDeMentira()

    avisos.Avisador(almacen.AlmacenMemoria(), agente, sincrono=True).modelos_nuevos([f"Modelo {i}" for i in range(35)])

    cuerpo = agente.avisos[0][1]
    assert "Modelo 29" in cuerpo and "Modelo 30" not in cuerpo and "y 5 más" in cuerpo and "35 modelos nuevos" in agente.avisos[0][0]


def test_si_la_base_falla_el_duenio_igual_entra_con_la_clave_de_arranque(monkeypatch):
    class Rota(almacen.AlmacenMemoria):
        def usuario(self, usuario):
            raise OSError("sin base")

    monkeypatch.setattr(acceso, "USUARIO", "leo")
    monkeypatch.setattr(acceso, "CLAVE", "clave-de-arranque-123")

    assert acceso.autenticar(Rota(), "leo", "clave-de-arranque-123") == {"usuario": "leo", "rol": "duenio"}
    assert acceso.autenticar(Rota(), "leo", "otra") is None
    assert acceso.autenticar(Rota(), "pablo", "lo-que-sea-123") is None, "una persona sin base no puede entrar"
