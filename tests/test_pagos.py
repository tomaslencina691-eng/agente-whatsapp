"""Pruebas de la verificación de pagos con Mercado Pago (mp.py, pagos.py y el panel). No salen a internet ni usan base.

Lo que se cuida acá:

  · Un cobro y un pedido van juntos solo si el monto es EXACTO y la pareja es única.
  · Con dudas (mismo total en dos pedidos, o dos cobros iguales) no se decide nada.
  · Un cobro se puede asignar una sola vez, y si el pedido no se puede confirmar el cobro queda libre.
  · En modo «sugerir» nada se confirma solo; en «auto», solo los cruces seguros.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mayorista"))

import acceso  # noqa: E402
import admin  # noqa: E402
import ajustes  # noqa: E402
import almacen  # noqa: E402
import historial as hist  # noqa: E402
import mp  # noqa: E402
import pagos  # noqa: E402
import tn  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

H = {"x-mayorista": "1"}
CLAVE = "clave-de-prueba-123"
AHORA = datetime.now(timezone.utc)


def iso(minutos=0):
    return (AHORA + timedelta(minutes=minutos)).isoformat()


def ped(codigo, total, estado="nuevo", minutos=0):
    return {"codigo": codigo, "total": total, "estado": estado, "fecha": iso(minutos)}


def cobro(id, monto, minutos=5):
    return {"id": id, "monto": monto, "fecha": iso(minutos), "pagador": {"nombre": "Pablo", "dni": "", "email": ""}, "tipo": "bank_transfer", "operacion": "money_transfer", "descripcion": ""}


# -- Cruzar cobros con pedidos ---------------------------------------------------------


def test_un_cobro_exacto_de_un_solo_pedido_es_un_cruce_seguro():
    r = pagos.cruzar([cobro("1", 201000)], [ped("A-AAAAA", 201000), ped("A-BBBBB", 99000)])

    assert r == {"1": {"pedidos": ["A-AAAAA"], "seguro": "A-AAAAA"}}


def test_un_monto_distinto_no_cruza_aunque_sea_parecido():
    assert pagos.cruzar([cobro("1", 200999)], [ped("A-AAAAA", 201000)]) == {"1": {"pedidos": [], "seguro": None}}


def test_dos_pedidos_con_el_mismo_total_dejan_la_duda_a_una_persona():
    r = pagos.cruzar([cobro("1", 100000)], [ped("A-AAAAA", 100000), ped("A-BBBBB", 100000)])

    assert r["1"] == {"pedidos": ["A-AAAAA", "A-BBBBB"], "seguro": None}


def test_dos_cobros_iguales_para_un_pedido_tampoco_son_seguros():
    r = pagos.cruzar([cobro("1", 100000), cobro("2", 100000)], [ped("A-AAAAA", 100000)])

    assert r["1"]["seguro"] is None and r["2"]["seguro"] is None


def test_un_pedido_sin_total_o_que_no_esta_nuevo_no_cruza():
    r = pagos.cruzar([cobro("1", 100000)], [ped("A-AAAAA", None), ped("A-BBBBB", 100000, estado="confirmado")])

    assert r["1"]["pedidos"] == []


def test_un_cobro_muy_anterior_al_pedido_no_cruza():
    r = pagos.cruzar([cobro("1", 100000, minutos=-300)], [ped("A-AAAAA", 100000)])

    assert r["1"]["pedidos"] == []


def test_un_cobro_unas_horas_antes_del_pedido_si_cruza():
    r = pagos.cruzar([cobro("1", 100000, minutos=-60)], [ped("A-AAAAA", 100000)])

    assert r["1"]["seguro"] == "A-AAAAA"


# -- Leer lo de Mercado Pago -----------------------------------------------------------


def pago_mp(**extra):
    return {"id": 123, "status": "approved", "transaction_amount": 201000.0, "collector_id": 77, "date_approved": "2026-11-17T09:10:11.000-03:00",
            "payer": {"first_name": "Pablo", "last_name": "Gómez", "identification": {"number": "30123456"}, "email": "p@x.com"},
            "payment_type_id": "bank_transfer", "operation_type": "money_transfer", **extra}


def test_un_cobro_aprobado_se_normaliza():
    n = mp.normalizar(pago_mp(), mi_id=77)

    assert n["id"] == "123" and n["monto"] == 201000.0 and n["fecha"].startswith("2026-11-17T12:10:11")
    assert n["pagador"] == {"nombre": "Pablo Gómez", "dni": "30123456", "email": "p@x.com"}


@pytest.mark.parametrize("cambio", [{"status": "pending"}, {"status": "rejected"}, {"status": "refunded"}, {"transaction_amount": 0}, {"collector_id": 999}])
def test_lo_que_no_es_plata_que_entro_se_descarta(cambio):
    assert mp.normalizar(pago_mp(**cambio), mi_id=77) is None


# -- El panel ----------------------------------------------------------------------------


class MercadoPagoFalso:
    ErrorDeMercadoPago = mp.ErrorDeMercadoPago

    def __init__(self):
        self.token = True
        self.cobros: list[dict] = []
        self.cae = False

    def configurado(self):
        return self.token

    def cuenta(self):
        return {"id": 77, "nombre": "Simplemente Fútbol", "email": "tienda@ejemplo.com"}

    def cobros_recientes(self, dias=3, limite=100, mi_id=None):
        if self.cae:
            raise mp.ErrorDeMercadoPago("Mercado Pago no contestó")
        return list(self.cobros)


@pytest.fixture
def mpf():
    return MercadoPagoFalso()


@pytest.fixture
def alm():
    a = almacen.AlmacenMemoria()
    a.cargar([(1, "M", 10), (1, "L", 1)])
    return a


@pytest.fixture
def web(monkeypatch, alm, mpf):
    monkeypatch.setattr(acceso, "USUARIO", "leo")
    monkeypatch.setattr(acceso, "CLAVE", CLAVE)
    monkeypatch.setattr(acceso, "SECRETO", "s" * 40)
    monkeypatch.setattr(tn, "TIENDA", "1")
    monkeypatch.setattr(tn, "TOKEN", "token")
    acceso._fallos.clear()
    defecto = ajustes.por_defecto({}, "Mi Tienda", "5491100000000", "", True, 6, [])
    app = FastAPI()
    router = admin.crear_router(hist.HistorialMemoria(), lambda: None, alm, [], lambda: dict(defecto), None, None, mpf)
    app.include_router(router)
    c = TestClient(app)
    assert c.post("/admin/api/login", json={"usuario": "leo", "clave": CLAVE}, headers=H).status_code == 200
    c.router_ = router
    return c


def hacer_pedido(alm, codigo, total, items=((1, "M", 2),)):
    datos = {"items": [{"producto": p, "talle": t, "cantidad": n, "nombre": "Boca"} for p, t, n in items], "nombre": "Pablo", "negocio": "", "notas": ""}
    alm.crear_pedido(codigo, datos, sum(n for _, _, n in items), total)


def activar(web, modo="sugerir"):
    r = web.put("/admin/api/pagos/config", json={"activo": True, "modo": modo}, headers=H)
    assert r.status_code == 200
    return r.json()


def test_apagado_no_se_revisa_nada(web):
    assert web.get("/admin/api/pagos").json()["activo"] is False
    assert web.post("/admin/api/pagos/revisar", headers=H).status_code == 409


def test_no_se_puede_activar_sin_el_token_del_servidor(web, mpf):
    mpf.token = False

    r = web.put("/admin/api/pagos/config", json={"activo": True, "modo": "sugerir"}, headers=H)

    assert r.status_code == 422 and "MERCADOPAGO_ACCESS_TOKEN" in r.json()["detail"]


def test_revisar_guarda_los_cobros_nuevos_y_no_duplica(web, alm, mpf):
    activar(web)
    mpf.cobros = [cobro("1", 55000), cobro("2", 66000)]

    assert web.post("/admin/api/pagos/revisar", headers=H).json() == {"revisado": True, "nuevos": 2, "confirmados": 0}
    assert web.post("/admin/api/pagos/revisar", headers=H).json()["nuevos"] == 0


def test_en_modo_sugerir_el_pedido_muestra_el_pago_detectado_pero_no_se_confirma_solo(web, alm, mpf):
    activar(web, "sugerir")
    hacer_pedido(alm, "A-AAAAA", 201000)
    mpf.cobros = [cobro("1", 201000)]

    r = web.post("/admin/api/pagos/revisar", headers=H).json()
    pedidos = web.get("/admin/api/pedidos").json()

    assert r["confirmados"] == 0 and alm.pedido("A-AAAAA")["estado"] == "nuevo"
    assert pedidos["pedidos"][0]["pagoDetectado"]["id"] == "1" and pedidos["pagos"]["porResolver"] == []


def test_un_cobro_sin_pedido_claro_queda_en_la_lista_para_resolver(web, alm, mpf):
    activar(web)
    hacer_pedido(alm, "A-AAAAA", 100000)
    hacer_pedido(alm, "A-BBBBB", 100000)
    mpf.cobros = [cobro("1", 100000)]
    web.post("/admin/api/pagos/revisar", headers=H)

    por_resolver = web.get("/admin/api/pedidos").json()["pagos"]["porResolver"]

    assert [(c["id"], sorted(c["pedidos"]), c["seguro"]) for c in por_resolver] == [("1", ["A-AAAAA", "A-BBBBB"], None)]


def test_asignar_confirma_el_pedido_y_descuenta_el_stock(web, alm, mpf):
    activar(web)
    hacer_pedido(alm, "A-AAAAA", 201000)
    mpf.cobros = [cobro("1", 201000)]
    web.post("/admin/api/pagos/revisar", headers=H)

    r = web.post("/admin/api/pagos/1/asignar", json={"codigo": "A-AAAAA"}, headers=H)

    assert r.status_code == 200 and r.json()["estado"] == "confirmado"
    assert alm.pedido("A-AAAAA")["estado"] == "confirmado"
    assert next(f["cantidad"] for f in alm.filas([1]) if f["talle"] == "M") == 8
    assert alm.pagos("asignado")[0]["pedido"] == "A-AAAAA"


def test_un_cobro_no_se_puede_usar_dos_veces(web, alm, mpf):
    activar(web)
    hacer_pedido(alm, "A-AAAAA", 100000)
    hacer_pedido(alm, "A-BBBBB", 100000)
    mpf.cobros = [cobro("1", 100000)]
    web.post("/admin/api/pagos/revisar", headers=H)
    web.post("/admin/api/pagos/1/asignar", json={"codigo": "A-AAAAA"}, headers=H)

    r = web.post("/admin/api/pagos/1/asignar", json={"codigo": "A-BBBBB"}, headers=H)

    assert r.status_code == 409 and alm.pedido("A-BBBBB")["estado"] == "nuevo"


def test_si_el_pedido_no_se_puede_confirmar_el_cobro_queda_libre(web, alm, mpf):
    activar(web)
    hacer_pedido(alm, "A-AAAAA", 201000, items=((1, "L", 5),))  # de L hay 1
    mpf.cobros = [cobro("1", 201000)]
    web.post("/admin/api/pagos/revisar", headers=H)

    r = web.post("/admin/api/pagos/1/asignar", json={"codigo": "A-AAAAA"}, headers=H)

    assert r.status_code == 409
    assert alm.pagos("detectado")[0]["id"] == "1" and alm.pedido("A-AAAAA")["estado"] == "nuevo"


def test_un_pedido_que_ya_no_esta_nuevo_no_se_puede_confirmar_con_un_cobro(web, alm, mpf):
    activar(web)
    hacer_pedido(alm, "A-AAAAA", 100000)
    alm.cancelar("A-AAAAA")
    mpf.cobros = [cobro("1", 100000)]
    web.post("/admin/api/pagos/revisar", headers=H)

    assert web.post("/admin/api/pagos/1/asignar", json={"codigo": "A-AAAAA"}, headers=H).status_code == 409


def test_en_modo_auto_se_confirman_solo_los_cruces_seguros(web, alm, mpf):
    activar(web, "auto")
    hacer_pedido(alm, "A-AAAAA", 201000)  # cruce seguro
    hacer_pedido(alm, "A-BBBBB", 100000)  # dos pedidos iguales: duda
    hacer_pedido(alm, "A-CCCCC", 100000)
    mpf.cobros = [cobro("1", 201000), cobro("2", 100000)]

    r = web.post("/admin/api/pagos/revisar", headers=H).json()

    assert r["confirmados"] == 1
    assert [alm.pedido(c)["estado"] for c in ("A-AAAAA", "A-BBBBB", "A-CCCCC")] == ["confirmado", "nuevo", "nuevo"]


def test_ignorar_un_cobro_lo_saca_de_la_lista(web, alm, mpf):
    activar(web)
    mpf.cobros = [cobro("1", 5000)]
    web.post("/admin/api/pagos/revisar", headers=H)

    assert web.post("/admin/api/pagos/1/ignorar", headers=H).json() == {"ignorado": True}
    assert web.get("/admin/api/pagos").json()["porResolver"] == []
    assert web.post("/admin/api/pagos/1/ignorar", headers=H).status_code == 409


def test_si_mercado_pago_se_cae_se_avisa_y_no_se_rompe_nada(web, alm, mpf):
    activar(web)
    mpf.cae = True

    r = web.post("/admin/api/pagos/revisar", headers=H)

    assert r.status_code == 502 and "Mercado Pago" in r.json()["detail"]


def test_probar_la_conexion_muestra_de_quien_es_el_token_y_los_ultimos_cobros(web, mpf):
    mpf.cobros = [cobro("1", 5000)]

    d = web.post("/admin/api/pagos/probar", headers=H).json()

    assert d["cuenta"]["nombre"] == "Simplemente Fútbol" and d["recientes"][0]["monto"] == 5000


def test_la_configuracion_de_pagos_es_solo_del_duenio():
    assert acceso.grupo_de("/admin/api/pagos/config") == "administrar"
    assert acceso.grupo_de("/admin/api/pagos/probar") == "administrar"
    assert acceso.grupo_de("/admin/api/pagos/1/asignar") == "stock"
