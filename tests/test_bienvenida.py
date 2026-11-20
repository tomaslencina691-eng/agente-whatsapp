"""La bienvenida: la manda el código en la primera respuesta, siempre y tal cual."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from agente import bienvenida  # noqa: E402
from test_chatwoot import TOKEN, ChatwootFalso, cliente, evento  # noqa: E402

PROMPT_CON_BIENVENIDA = (
    "Sos el asistente.\n\n"
    "[BIENVENIDA]\n"
    "¡Hola! Soy Tomi.\n"
    "Catálogo: https://mayoristasf.digital\n"
    "[FIN DE LA BIENVENIDA]\n\n"
    "Más reglas.\n"
)
BIENVENIDA = "¡Hola! Soy Tomi.\nCatálogo: https://mayoristasf.digital"


def _prompt(tmp_path):
    ruta = tmp_path / "sistema.md"
    ruta.write_text(PROMPT_CON_BIENVENIDA, encoding="utf-8")
    return ruta


# -- Las piezas -------------------------------------------------------------------


def test_la_bienvenida_se_saca_de_entre_las_marcas():
    assert bienvenida.extraer(PROMPT_CON_BIENVENIDA) == BIENVENIDA
    assert bienvenida.extraer("Un prompt sin marcas.") == ""


def test_armar_la_primera_respuesta():
    assert bienvenida.armar("Hola", "De Boca hay M.") == "Hola\n\nDe Boca hay M."
    assert bienvenida.armar("Hola", "[sin_respuesta]") == "Hola"
    assert bienvenida.armar("", "De Boca hay M.") == "De Boca hay M."


def test_el_pedido_de_la_web_no_corresponde():
    assert not bienvenida.corresponde("PEDIDO MAYORISTA A-1")
    assert bienvenida.corresponde("hola")


def test_el_prompt_del_repositorio_tiene_la_bienvenida():
    texto = (Path(__file__).parent.parent / "prompts" / "sistema.md").read_text(encoding="utf-8")
    b = bienvenida.extraer(texto)
    assert b.startswith("¡Hola! ¿Cómo estás?")
    assert "mayoristasf.digital" in b and "[FIN" not in b


# -- De punta a punta, por el webhook ------------------------------------------------


def test_la_primera_respuesta_lleva_la_bienvenida_arriba(tmp_path):
    from test_agente import agente_falso

    canal = ChatwootFalso()
    agente = agente_falso(["De Boca hay alternativa en M.", "segunda"])

    with cliente(canal, agente, prompt_sistema=_prompt(tmp_path), mensajes_por_respuesta=1) as web:
        web.post(f"/chatwoot/{TOKEN}", json=evento("me interesan las de boca", id_mensaje=1))
        web.post(f"/chatwoot/{TOKEN}", json=evento("y en L?", id_mensaje=2))

    assert canal.envios() == [
        BIENVENIDA + "\n\nDe Boca hay alternativa en M.",
        "segunda",
    ], "la bienvenida va una sola vez, en la primera respuesta"
    primero = [m.content for m in agente.historial("12") if m.type == "human"][0]
    assert "la bienvenida" in primero, "el modelo sabe que la bienvenida ya sale"


def test_si_solo_saludo_sale_solo_la_bienvenida(tmp_path):
    from test_agente import agente_falso

    canal = ChatwootFalso()

    with cliente(canal, agente_falso(["[sin_respuesta]"]), prompt_sistema=_prompt(tmp_path), mensajes_por_respuesta=1) as web:
        web.post(f"/chatwoot/{TOKEN}", json=evento("hola"))

    assert canal.envios() == [BIENVENIDA]


def test_el_pedido_de_la_web_no_lleva_bienvenida(tmp_path):
    from test_agente import agente_falso

    canal = ChatwootFalso()

    with cliente(canal, agente_falso(["Recibimos tu pedido A-7K3QX."]), prompt_sistema=_prompt(tmp_path)) as web:
        web.post(f"/chatwoot/{TOKEN}", json=evento("PEDIDO MAYORISTA A-7K3QX\n- Boca M x6"))

    assert canal.envios() == ["Recibimos tu pedido A-7K3QX."]
