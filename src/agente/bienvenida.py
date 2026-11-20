"""El primer mensaje de cada conversación: la bienvenida, siempre y tal cual.

Dejarla en manos del modelo no alcanza: si la persona arranca preguntando
algo concreto ("me interesan las de Boca"), el modelo a veces contesta eso y
se saltea la bienvenida. Por eso la manda el código: en la primera respuesta
de una conversación, la bienvenida va arriba y la respuesta del modelo abajo,
en el mismo mensaje.

El texto vive en el prompt (así se edita desde el panel, como todo lo demás),
entre dos marcas en renglones propios:

    [BIENVENIDA]
    ¡Hola! ¿Cómo estás? ...
    [FIN DE LA BIENVENIDA]

Si el prompt no tiene las marcas, no se manda nada aparte: rige lo que diga
el prompt, como antes.
"""

from __future__ import annotations

import re

INICIO = "[BIENVENIDA]"
FIN = "[FIN DE LA BIENVENIDA]"

_BLOQUE = re.compile(
    r"^[ \t]*\[BIENVENIDA\][ \t]*\n(.*?)\n[ \t]*\[FIN DE LA BIENVENIDA\][ \t]*$",
    re.DOTALL | re.MULTILINE,
)

# Lo que el modelo contesta cuando la persona solo saludó: la bienvenida ya
# responde todo y no hace falta agregar nada abajo.
_SIN_RESPUESTA = re.compile(r"\[\s*sin[_ ]respuesta\s*\]", re.IGNORECASE)

# Va pegado al primer mensaje de la persona, para que el modelo sepa que la
# bienvenida ya sale y no la repita. Entre corchetes: el prompt dice que eso
# lo escribe el sistema, no la persona.
AVISO = (
    "\n\n[Aviso del sistema: la bienvenida (catálogo, cómo comprar y precios) ya "
    "se le manda en este mismo mensaje, arriba de tu respuesta. No la repitas ni "
    "vuelvas a saludar ni a presentarte. Si la persona preguntó algo concreto, "
    "respondelo. Si solo saludó o pidió información en general, respondé "
    "únicamente: [sin_respuesta]]"
)


def extraer(prompt: str) -> str:
    """El texto de la bienvenida que está en el prompt, o "" si no tiene las marcas."""
    encontrado = _BLOQUE.search(prompt or "")
    return encontrado.group(1).strip() if encontrado else ""


def corresponde(texto: str) -> bool:
    """Si a este primer mensaje le va la bienvenida.

    El pedido armado en la web no: esa persona ya vio el catálogo y los
    precios, y lo que espera es la respuesta a su pedido.
    """
    return not (texto or "").lstrip().upper().startswith("PEDIDO MAYORISTA")


def armar(bienvenida: str, respuesta: str) -> str:
    """La bienvenida arriba y lo que contestó el modelo abajo (si contestó algo)."""
    respuesta = _SIN_RESPUESTA.sub("", respuesta or "").strip()
    if not bienvenida:
        return respuesta
    return bienvenida + ("\n\n" + respuesta if respuesta else "")
