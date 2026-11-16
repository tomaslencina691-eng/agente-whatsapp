"""Pruebas de lo que el panel de administración le cambia al agente. No gastan tokens ni usan base.

  · panel_db.py: el prompt, los ajustes y los datos del negocio que el agente lee en vivo.
  · webhook.py: pausar el agente, el tope y los mensajes por respuesta desde el panel, y las
    dos puertas internas (/panel/estado y /panel/probar).

    pytest
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mayorista"))

from agente import panel_db  # noqa: E402
from agente.panel_db import SIN_DEFINIR, PanelEnVivo, aplicar_datos  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402

from test_agente import agente_falso  # noqa: E402
from test_chatwoot import TOKEN, ChatwootFalso, cliente, esperar, evento  # noqa: E402

SECRETO = "secreto-compartido-con-el-panel-123"


class PanelFalso(PanelEnVivo):
    """Lo que el panel tendría guardado, sin base."""

    def __init__(self, prompt=None, ajustes=None, datos=None):
        super().__init__("")
        self._prompt, self._ajustes, self._datos = prompt, ajustes or {}, datos or {}


# -- Los datos del negocio dentro del prompt ----------------------------------------


def test_los_datos_del_negocio_se_ponen_en_el_prompt():
    texto = aplicar_datos("El alias es {{alias_pago}} y atendemos {{horario}}.", {"alias_pago": " sf.pagos ", "horario": "de 9 a 18"})

    assert texto == "El alias es sf.pagos y atendemos de 9 a 18."


def test_un_dato_sin_cargar_no_queda_como_variable_en_el_prompt():
    texto = aplicar_datos("Alias: {{alias_pago}}", {"alias_pago": "   "})

    assert texto == f"Alias: {SIN_DEFINIR}"
    assert "{{" not in texto


def test_una_variable_desconocida_se_deja_como_esta():
    assert aplicar_datos("Hola {{inventada}}", {}) == "Hola {{inventada}}"


def test_el_panel_y_el_agente_usan_las_mismas_variables():
    import admin

    assert admin.VARIABLES_DEL_AGENTE == panel_db.VARIABLES


# -- La lectura de la base ----------------------------------------------------------


class PanelContado(PanelEnVivo):
    """Cuenta las lecturas y falla cuando se le pide, para probar la caché."""

    def __init__(self, reloj, **datos):
        super().__init__("postgresql://no-se-usa", segundos=20, reloj=reloj)
        self.lecturas = 0
        self.falla = False
        self.nuevo = datos

    def _leer(self):
        self.lecturas += 1
        if self.falla:
            raise OSError("sin base")
        self._prompt = self.nuevo.get("prompt")
        self._ajustes = self.nuevo.get("ajustes", {})
        self._datos = self.nuevo.get("datos", {})


def test_la_base_se_lee_cada_veinte_segundos_y_no_en_cada_mensaje():
    hora = [0.0]
    p = PanelContado(lambda: hora[0], prompt="uno")

    assert p.prompt() == "uno" and p.prompt() == "uno"
    assert p.lecturas == 1
    hora[0] = 21
    p.nuevo = {"prompt": "dos"}

    assert p.prompt() == "dos" and p.lecturas == 2


def test_si_la_base_falla_el_agente_sigue_con_lo_ultimo_que_supo():
    hora = [0.0]
    p = PanelContado(lambda: hora[0], prompt="uno", ajustes={"activo": False})
    assert p.prompt() == "uno"
    hora[0], p.falla = 30, True

    assert p.prompt() == "uno" and p.ajustes() == {"activo": False}


def test_si_la_base_falla_desde_el_arranque_rige_el_del_repositorio():
    p = PanelContado(lambda: 0.0)
    p.falla = True

    assert p.prompt() is None and p.ajustes() == {} and p.datos() == {}


def test_sin_base_el_panel_no_hace_nada():
    p = PanelEnVivo("")

    assert (p.prompt(), p.ajustes(), p.datos()) == (None, {}, {})


def test_los_ajustes_guardados_pisan_a_los_del_entorno():
    agente = agente_falso(["x"])
    agente.config.mensajes_por_respuesta, agente.config.tope_mensajes_por_dia = 3, 50
    p = PanelFalso(ajustes={"mensajesPorRespuesta": 1, "campoViejo": 9})

    v = p.valores(agente.config)

    assert v["mensajesPorRespuesta"] == 1 and v["topeMensajesPorDia"] == 50 and v["activo"] is True
    assert "campoViejo" not in v


# -- El prompt del agente -------------------------------------------------------------


def texto_del_sistema(a) -> str:
    """El texto del prompt, venga como texto o como bloques marcados para el caché (Claude)."""
    contenido = a._sistema().content
    return contenido if isinstance(contenido, str) else "".join(b["text"] for b in contenido)


def test_el_agente_usa_el_prompt_del_panel_con_los_datos_puestos():
    a = agente_falso(["x"])
    a.panel = PanelFalso(prompt="Alias: {{alias_pago}}", datos={"alias_pago": "sf.pagos"})

    assert texto_del_sistema(a) == "Alias: sf.pagos"


def test_sin_prompt_en_el_panel_el_agente_usa_el_del_repositorio():
    a = agente_falso(["x"])
    a.panel = PanelFalso(prompt=None)

    assert texto_del_sistema(a).startswith("Sos el asistente virtual de Simplemente Fútbol")


# -- El webhook con ajustes en vivo ---------------------------------------------------


def con_panel(agente, panel):
    agente.panel = panel
    return agente


def test_con_el_agente_en_pausa_no_contesta_y_pasa_la_charla_a_una_persona_una_sola_vez():
    canal = ChatwootFalso()
    agente = con_panel(agente_falso(["no debería usarse"]), PanelFalso(ajustes={"activo": False}))

    with cliente(canal, agente) as web:
        estados = [web.post(f"/chatwoot/{TOKEN}", json=evento("hola", id_mensaje=i)).json()["estado"] for i in (1, 2, 3)]
        assert esperar(lambda: canal.etiquetas_mandadas())

    assert estados == ["pausado"] * 3
    assert canal.envios() == []
    assert canal.etiquetas_mandadas() == [["humano"]], "la etiqueta se pone una vez por conversación, no por mensaje"


def test_el_agente_vuelve_a_contestar_cuando_se_lo_reactiva():
    canal = ChatwootFalso()
    panel = PanelFalso(ajustes={"activo": False})
    agente = con_panel(agente_falso(["listo"]), panel)

    with cliente(canal, agente) as web:
        assert web.post(f"/chatwoot/{TOKEN}", json=evento("hola", id_mensaje=1)).json()["estado"] == "pausado"
        panel._ajustes = {"activo": True}
        assert web.post(f"/chatwoot/{TOKEN}", json=evento("hola", id_mensaje=2, conversacion=99)).json()["estado"] == "recibido"
        assert esperar(lambda: canal.envios())

    assert canal.envios() == ["listo"]


def test_los_mensajes_por_respuesta_del_panel_rigen_en_vivo():
    larga = "Hola, ¿cómo estás?\n\nTe cuento: tengo la camiseta en M y en L.\n\nDecime cuántas querés."
    canal = ChatwootFalso()
    agente = con_panel(agente_falso([larga]), PanelFalso(ajustes={"mensajesPorRespuesta": 1}))

    with cliente(canal, agente) as web:
        web.post(f"/chatwoot/{TOKEN}", json=evento("hola"))
        assert esperar(lambda: canal.envios())

    assert len(canal.envios()) == 1, "el .env decía 3 pero el panel dice 1"


def test_el_tope_del_panel_rige_en_vivo():
    canal = ChatwootFalso()
    agente = con_panel(agente_falso(["uno", "dos", "tres"]), PanelFalso(ajustes={"topeMensajesPorDia": 1}))

    with cliente(canal, agente) as web:
        estados = [web.post(f"/chatwoot/{TOKEN}", json=evento("hola", id_mensaje=i)).json()["estado"] for i in (1, 2)]
        assert esperar(lambda: canal.etiquetas_mandadas())

    assert estados == ["recibido", "tope"]


def test_el_mail_de_avisos_del_panel_pisa_al_del_entorno():
    from test_chatwoot import AvisosFalsos

    avisos = AvisosFalsos()
    canal = ChatwootFalso()
    agente = con_panel(agente_falso(["x"]), PanelFalso(ajustes={"avisosEmail": "otro@ejemplo.com"}))

    with cliente(canal, agente, avisos=avisos) as web:
        web.post(f"/chatwoot/{TOKEN}", json=evento("hola"))
        assert esperar(lambda: canal.envios())

    assert avisos.para == "otro@ejemplo.com"


# -- Las puertas internas del panel ---------------------------------------------------


@pytest.fixture
def secreto(monkeypatch):
    monkeypatch.setenv("AGENTE_PANEL_TOKEN", SECRETO)


H = {"x-panel-token": SECRETO}


def test_sin_secreto_configurado_el_panel_esta_apagado(monkeypatch):
    monkeypatch.delenv("AGENTE_PANEL_TOKEN", raising=False)

    with cliente(ChatwootFalso(), agente_falso(["x"])) as web:
        assert web.get("/panel/estado", headers=H).status_code == 503
        assert web.post("/panel/probar", headers=H, json={}).status_code == 503


@pytest.mark.parametrize("cabeceras", [{}, {"x-panel-token": "otro"}, {"x-panel-token": SECRETO + "x"}])
def test_con_otro_secreto_no_se_entra(secreto, cabeceras):
    with cliente(ChatwootFalso(), agente_falso(["x"])) as web:
        assert web.get("/panel/estado", headers=cabeceras).status_code == 401
        assert web.post("/panel/probar", headers=cabeceras, json={"prompt": "p" * 30, "mensajes": [{"rol": "yo", "texto": "hola"}]}).status_code == 401


def test_el_estado_trae_el_modelo_el_prompt_del_repositorio_y_los_ajustes(secreto):
    agente = agente_falso(["x"])
    agente.config.mensajes_por_respuesta = 2

    with cliente(ChatwootFalso(), agente) as web:
        d = web.get("/panel/estado", headers=H).json()

    assert d["modelo"] == "modelo-de-prueba"
    assert d["prompt_base"].startswith("Sos el asistente virtual de Simplemente Fútbol")
    assert d["base"]["mensajesPorRespuesta"] == 2 and d["vivos"]["activo"] is True
    assert d["variables"] == list(panel_db.VARIABLES)


class ModeloQueAnota:
    """Un modelo que anota lo que le llega y contesta lo que se le pide."""

    def __init__(self, *respuestas):
        self.respuestas = list(respuestas)
        self.entradas = []

    def bind_tools(self, *a, **k):
        return self

    def invoke(self, entrada):
        self.entradas.append(list(entrada))
        return self.respuestas.pop(0)


def probar(web, **cuerpo):
    cuerpo.setdefault("prompt", "Sos un asistente de prueba. Alias: {{alias_pago}}.")
    cuerpo.setdefault("mensajes", [{"rol": "yo", "texto": "hola"}])
    return web.post("/panel/probar", headers=H, json=cuerpo)


def test_una_prueba_contesta_con_el_prompt_del_editor_sin_tocar_ninguna_conversacion(secreto):
    agente = agente_falso(["x"])
    agente.modelo = ModeloQueAnota(AIMessage("¡Buenas! Te paso el alias."))

    with cliente(ChatwootFalso(), agente) as web:
        r = probar(web, datos={"alias_pago": "sf.pagos"})

    assert r.status_code == 200 and r.json()["texto"] == "¡Buenas! Te paso el alias."
    sistema = agente.modelo.entradas[0][0].content
    assert sistema == "Sos un asistente de prueba. Alias: sf.pagos."
    assert agente.historial("probador") == [], "no se guardó nada en ninguna memoria"


def test_la_prueba_parte_la_respuesta_como_se_mandaria_por_whatsapp(secreto):
    larga = "Hola, ¿cómo estás?\n\nTe cuento: tengo la camiseta en M y en L.\n\nDecime cuántas querés."
    agente = agente_falso(["x"])
    agente.modelo = ModeloQueAnota(AIMessage(larga))

    with cliente(ChatwootFalso(), agente) as web:
        d = probar(web).json()

    assert len(d["mensajes"]) == 3 and d["herramientas"] == []


def test_la_prueba_usa_las_herramientas_de_verdad(secreto, monkeypatch):
    from agente import agente as modulo
    from langchain_core.tools import tool

    @tool
    def buscar_producto(consulta: str) -> str:
        """Busca productos."""
        return "Barcelona 26/27: M (2 u.), L (7 u.)"

    monkeypatch.setattr(modulo, "HERRAMIENTAS", [buscar_producto])
    agente = agente_falso(["x"])
    agente.modelo = ModeloQueAnota(
        AIMessage("", tool_calls=[{"name": "buscar_producto", "args": {"consulta": "barcelona"}, "id": "t1"}]),
        AIMessage("De Barcelona tengo L (7 u.)."),
    )

    with cliente(ChatwootFalso(), agente) as web:
        d = probar(web, mensajes=[{"rol": "yo", "texto": "tenés barcelona?"}]).json()

    assert d["herramientas"] == ["buscar_producto"] and d["texto"] == "De Barcelona tengo L (7 u.)."
    assert "L (7 u.)" in agente.modelo.entradas[1][-1].content, "el modelo recibió lo que devolvió la herramienta"


def test_si_el_modelo_falla_la_prueba_lo_dice(secreto):
    agente = agente_falso(["x"])

    class Roto(ModeloQueAnota):
        def invoke(self, entrada):
            raise RuntimeError("sin crédito")

    agente.modelo = Roto()

    with cliente(ChatwootFalso(), agente) as web:
        r = probar(web)

    assert r.status_code == 502 and "sin crédito" in r.json()["error"]


@pytest.mark.parametrize(
    "cuerpo",
    [
        {},
        {"prompt": "", "mensajes": [{"rol": "yo", "texto": "hola"}]},
        {"prompt": "p" * 30001, "mensajes": [{"rol": "yo", "texto": "hola"}]},
        {"prompt": "p" * 30, "mensajes": []},
        {"prompt": "p" * 30, "mensajes": [{"rol": "agente", "texto": "hola"}]},  # tiene que terminar con la persona
        {"prompt": "p" * 30, "mensajes": [{"rol": "otro", "texto": "hola"}]},
    ],
)
def test_pedidos_de_prueba_mal_armados_no_llegan_al_modelo(secreto, cuerpo):
    agente = agente_falso(["x"])
    agente.modelo = ModeloQueAnota(AIMessage("no debería usarse"))

    with cliente(ChatwootFalso(), agente) as web:
        r = web.post("/panel/probar", headers=H, json=cuerpo)

    assert r.status_code == 400 and agente.modelo.entradas == []


# -- Avisos del panel por mail ----------------------------------------------------------------


def test_el_estado_dice_con_que_esta_conectado(secreto):
    from test_chatwoot import AvisosFalsos

    with cliente(ChatwootFalso(), agente_falso(["x"]), avisos=AvisosFalsos()) as web:
        c = web.get("/panel/estado", headers=H).json()["conexiones"]

    assert set(c) == {"chatwoot", "whatsapp", "avisos", "avisos_para"}
    assert c["avisos"] is True and c["avisos_para"] == "duenio@ejemplo.com" and c["whatsapp"] is False


def test_el_panel_manda_un_aviso_por_el_mismo_camino_que_los_del_agente(secreto):
    from test_chatwoot import AvisosFalsos

    avisos = AvisosFalsos()

    with cliente(ChatwootFalso(), agente_falso(["x"]), avisos=avisos) as web:
        r = web.post("/panel/avisar", headers=H, json={"asunto": "Pedido nuevo A-AAAAA", "cuerpo": "Entró un pedido.", "tipo": "pedido:A-AAAAA"})

    assert r.status_code == 200 and r.json() == {"enviado": True, "activos": True}
    assert avisos.mandados == [("Pedido nuevo A-AAAAA", "Entró un pedido.")]


def test_un_aviso_repetido_no_sale_otra_vez(secreto):
    from test_chatwoot import AvisosFalsos

    avisos = AvisosFalsos()
    cuerpo = {"asunto": "Quedan pocas unidades", "cuerpo": "Boca: 1 u.", "tipo": "poco-stock:Boca"}

    with cliente(ChatwootFalso(), agente_falso(["x"]), avisos=avisos) as web:
        primero = web.post("/panel/avisar", headers=H, json=cuerpo).json()["enviado"]
        segundo = web.post("/panel/avisar", headers=H, json=cuerpo).json()["enviado"]

    assert (primero, segundo) == (True, False) and len(avisos.mandados) == 1


@pytest.mark.parametrize("cuerpo", [{}, {"asunto": "", "cuerpo": "x", "tipo": "t"}, {"asunto": "x", "cuerpo": "", "tipo": "t"}, {"asunto": "x", "cuerpo": "y", "tipo": ""}])
def test_avisos_mal_armados_no_salen(secreto, cuerpo):
    from test_chatwoot import AvisosFalsos

    avisos = AvisosFalsos()

    with cliente(ChatwootFalso(), agente_falso(["x"]), avisos=avisos) as web:
        r = web.post("/panel/avisar", headers=H, json=cuerpo)

    assert r.status_code == 400 and avisos.mandados == []


def test_sin_secreto_no_se_puede_mandar_avisos(secreto):
    from test_chatwoot import AvisosFalsos

    avisos = AvisosFalsos()

    with cliente(ChatwootFalso(), agente_falso(["x"]), avisos=avisos) as web:
        assert web.post("/panel/avisar", json={"asunto": "a", "cuerpo": "b", "tipo": "c"}).status_code == 401
        assert web.post("/panel/mail-de-prueba").status_code == 401

    assert avisos.mandados == []


def test_el_mail_de_prueba_sale_y_dice_a_quien(secreto):
    from test_chatwoot import AvisosFalsos

    avisos = AvisosFalsos()

    with cliente(ChatwootFalso(), agente_falso(["x"]), avisos=avisos) as web:
        r = web.post("/panel/mail-de-prueba", headers=H).json()

    assert r == {"ok": True, "para": "duenio@ejemplo.com"} and len(avisos.mandados) == 1


def test_si_el_mail_no_esta_configurado_la_prueba_dice_que_falta(secreto):
    from agente.avisos import Avisos

    with cliente(ChatwootFalso(), agente_falso(["x"]), avisos=Avisos()) as web:
        r = web.post("/panel/mail-de-prueba", headers=H).json()

    assert r["ok"] is False and "Faltan datos del mail" in r["error"]


def test_el_mail_de_avisos_guardado_en_el_panel_rige_tambien_para_los_avisos_del_panel(secreto):
    from test_chatwoot import AvisosFalsos

    avisos = AvisosFalsos()
    agente = agente_falso(["x"])
    agente.panel = PanelFalso(ajustes={"avisosEmail": "nuevo@ejemplo.com"})

    with cliente(ChatwootFalso(), agente, avisos=avisos) as web:
        antes = web.get("/panel/estado", headers=H).json()["conexiones"]["avisos_para"]
        prueba = web.post("/panel/mail-de-prueba", headers=H).json()

    assert antes == "nuevo@ejemplo.com" and prueba == {"ok": True, "para": "nuevo@ejemplo.com"}
