"""Las herramientas: lo que el agente puede hacer además de conversar.

Una herramienta es una función común de Python que el modelo puede decidir
llamar. El modelo no la ejecuta: dice "quiero llamar a clima con lugar=Rosario"
y LangGraph la corre y le devuelve el resultado. Por eso el **docstring importa
tanto como el código**: es literalmente lo único que el modelo lee para decidir
si esta herramienta le sirve y qué mandarle.

Hay dos grupos:

- **El clima**, con Open-Meteo (https://open-meteo.com): gratis, sin clave.
- **La tienda** (Tiendanube), con `buscar_producto` y `consultar_pedido`: leen
  la tienda real del negocio. Solo hacen falta si están cargadas
  TIENDANUBE_STORE_ID y TIENDANUBE_ACCESS_TOKEN — si no, esas dos herramientas
  igual están en la lista, pero avisan que la tienda no está conectada en vez
  de romper.

Todas usan `urllib`, de la biblioteca estándar, para no sumar dependencias al
requirements.txt por unos pedidos HTTP.
"""

from __future__ import annotations

import difflib
import json
import os
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from langchain_core.tools import tool

GEOCODING = "https://geocoding-api.open-meteo.com/v1/search"
PRONOSTICO = "https://api.open-meteo.com/v1/forecast"

# Si Open-Meteo no contesta en este tiempo, cortamos. Sin un tope, una consulta
# colgada deja al agente mudo en la mitad de la conversación.
ESPERA = 15

# Open-Meteo devuelve el estado del cielo como un número (el código WMO, un
# estándar meteorológico). Esta es la traducción a algo que una persona lea.
CIELO = {
    0: "despejado",
    1: "casi despejado",
    2: "parcialmente nublado",
    3: "nublado",
    45: "con niebla",
    48: "con niebla que escarcha",
    51: "con llovizna suave",
    53: "con llovizna",
    55: "con llovizna fuerte",
    56: "con llovizna helada",
    57: "con llovizna helada fuerte",
    61: "con lluvia suave",
    63: "con lluvia",
    65: "con lluvia fuerte",
    66: "con lluvia helada",
    67: "con lluvia helada fuerte",
    71: "con nevada suave",
    73: "con nevada",
    75: "con nevada fuerte",
    77: "con granizo fino",
    80: "con chaparrones",
    81: "con chaparrones fuertes",
    82: "con chaparrones muy fuertes",
    85: "con chaparrones de nieve",
    86: "con chaparrones de nieve fuertes",
    95: "con tormenta",
    96: "con tormenta y algo de granizo",
    99: "con tormenta y granizo",
}


@tool
def clima(lugar: str) -> str:
    """Dice el clima que hace ahora mismo en una ciudad.

    Usala cuando te pregunten por el clima, la temperatura, si llueve, si hace
    frío o calor, o si conviene salir con abrigo o paraguas.

    Args:
        lugar: La ciudad, en lo posible con el país. Por ejemplo "Rosario",
            "Buenos Aires, Argentina" o "Madrid". Si hay varias ciudades con
            el mismo nombre, se toma la más conocida.
    """
    # Por qué la herramienta devuelve el error en vez de levantarlo (esta es la
    # cuarta falla que se traga el proyecto, y va con el motivo escrito al lado,
    # como pide AGENTS.md): si una herramienta explota, LangGraph corta toda la
    # respuesta y la persona ve un error crudo. Devolviéndolo como texto, el
    # resultado le llega al modelo, que lo cuenta con sus palabras y la
    # conversación sigue. Ojo con la diferencia: acá no se esconde nada, el
    # problema igual termina en la pantalla — pero explicado y sin voltear la
    # charla. Los errores del *proveedor* siguen saliendo tal cual: esto es
    # una consulta a Open-Meteo, no al modelo.
    try:
        encontrado = _buscar_lugar(lugar)
    except Exception as e:
        return f"No se pudo consultar el clima de '{lugar}': {type(e).__name__}: {e}"

    if encontrado is None:
        return (
            f"No encontré ninguna ciudad que se llame '{lugar}'. "
            "Puede estar mal escrita, o convenir agregarle el país."
        )

    try:
        datos = _pedir_el_clima(encontrado["latitud"], encontrado["longitud"])
    except Exception as e:
        return f"No se pudo consultar el clima de '{lugar}': {type(e).__name__}: {e}"

    return _redactar(encontrado, datos)


@tool
def buscar_producto(consulta: str) -> str:
    """Busca productos reales de la tienda: precio, talles con stock del mayorista y el link.

    Usala cuando alguien pregunte por una camiseta o producto, si hay stock,
    cuánto sale, o pida una recomendación. Buscá con las palabras que usó la
    persona tal cual salieron, aunque estén mal escritas o incompletas (por
    ejemplo "camiseta argentna" o "boca titular 25") — esta herramienta
    tolera bastante el error de tipeo sola, así que no hace falta corregir
    nada antes de llamarla.

    Si el resultado viene sin stock, la respuesta ya trae alternativas con
    stock — ofrecelas antes de que la persona pregunte de nuevo.

    Args:
        consulta: lo que la persona busca, en sus propias palabras.
    """
    if not _tiendanube_configurado():
        return "La tienda todavía no está conectada a este agente."

    try:
        stock = _stock_mayorista()
        productos = _buscar_en_catalogo(consulta, stock=stock)
    except Exception as e:
        return f"No se pudo consultar la tienda: {type(e).__name__}: {e}"

    if not productos:
        return (
            f"No encontré ningún producto que coincida con '{consulta}'. "
            "Puede estar mal escrito o no ser algo que vendamos."
        )

    return "\n\n".join(_describir_producto(p, stock) for p in productos)


@tool
def consultar_pedido(nombre: str, correo: str, numero_orden: str) -> str:
    """Consulta el estado de un pedido ya hecho: envío, seguimiento, en qué va.

    Usala solo para pedidos YA HECHOS (no para elegir qué comprar, para eso
    está buscar_producto). Antes de llamarla, pedile a la persona los tres
    datos — nombre completo, correo, y número de orden (el que le llegó por
    mail al comprar) — y no llames a la herramienta hasta tener los tres.

    El número de orden es la clave de todo: sin el número correcto no se
    entrega ninguna información, aunque el nombre y el correo sean
    perfectos. Si la herramienta dice que no pudo verificar el pedido, no
    inventes ni supongas nada — pedile que revise los datos, o derivá a una
    persona del equipo si insiste en que están bien.

    Args:
        nombre: nombre completo que dio la persona.
        correo: el correo que dio la persona.
        numero_orden: el número de orden que dio la persona (solo el número,
            sin el "#").
    """
    if not _tiendanube_configurado():
        return "La tienda todavía no está conectada a este agente."

    numero = "".join(c for c in numero_orden if c.isdigit())
    if not numero:
        return (
            "Ese número de orden no parece válido. Pedile que te pase el que "
            "le llegó por mail al comprar."
        )

    try:
        candidatos = _tiendanube_pedir("/orders", {"q": numero, "per_page": 10})
    except Exception as e:
        return f"No se pudo consultar el pedido: {type(e).__name__}: {e}"

    pedido = next((o for o in candidatos if str(o.get("number")) == numero), None)

    # Mismo mensaje si falla el número o si falla el nombre/correo: no hay
    # que darle a nadie una pista de cuál de los dos datos estuvo mal.
    if pedido is None or not _coincide_con_el_pedido(pedido, nombre, correo):
        return (
            "No pudimos verificar ese pedido con los datos que nos diste. "
            "Revisá que el nombre, el correo y el número de orden sean "
            "exactamente los de esa compra."
        )

    return _describir_pedido(pedido)


# Lo que el agente tiene atado. Cuando agregues otra herramienta, sumala acá:
# es la única lista que mira el grafo.
HERRAMIENTAS = [clima, buscar_producto, consultar_pedido]


# -- Las consultas ------------------------------------------------------------


def _buscar_lugar(nombre: str) -> dict | None:
    """Convierte un nombre de ciudad en coordenadas. None si no existe."""
    datos = _traer(
        GEOCODING,
        {"name": nombre, "count": 1, "language": "es", "format": "json"},
    )

    resultados = datos.get("results") or []
    if not resultados:
        return None

    primero = resultados[0]
    return {
        "nombre": primero.get("name") or nombre,
        # admin1 es la provincia o el estado. Sirve para desambiguar cuando hay
        # cinco ciudades con el mismo nombre en países distintos.
        "provincia": primero.get("admin1") or "",
        "pais": primero.get("country") or "",
        "latitud": primero["latitude"],
        "longitud": primero["longitude"],
    }


def _pedir_el_clima(latitud: float, longitud: float) -> dict:
    """El clima de este momento en esas coordenadas."""
    return _traer(
        PRONOSTICO,
        {
            "latitude": latitud,
            "longitude": longitud,
            "current": (
                "temperature_2m,relative_humidity_2m,apparent_temperature,"
                "weather_code,wind_speed_10m"
            ),
            # timezone=auto hace que la hora venga en la del lugar consultado,
            # no en la nuestra. Si preguntás por Tokio querés la hora de Tokio.
            "timezone": "auto",
        },
    )


def _traer(url: str, parametros: dict) -> dict:
    """Un GET que devuelve JSON."""
    completa = f"{url}?{urllib.parse.urlencode(parametros)}"

    with urllib.request.urlopen(completa, timeout=ESPERA) as respuesta:
        return json.loads(respuesta.read().decode("utf-8"))


# -- El texto que lee el modelo -----------------------------------------------


def _redactar(lugar: dict, datos: dict) -> str:
    """Arma la respuesta en texto.

    Le devolvemos al modelo una frase escrita, no el JSON crudo: entiende
    cualquiera de los dos, pero con el texto ya redactado es mucho menos
    probable que se equivoque de unidad o invente un dato que no está.
    """
    ahora = datos.get("current") or {}

    partes = [f"Clima en {_nombre_completo(lugar)}:"]
    partes.append(f"- Temperatura: {ahora.get('temperature_2m', '?')} °C")

    sensacion = ahora.get("apparent_temperature")
    if sensacion is not None:
        partes.append(f"- Sensación térmica: {sensacion} °C")

    partes.append(f"- Cielo: {_describir_cielo(ahora.get('weather_code'))}")

    humedad = ahora.get("relative_humidity_2m")
    if humedad is not None:
        partes.append(f"- Humedad: {humedad} %")

    viento = ahora.get("wind_speed_10m")
    if viento is not None:
        partes.append(f"- Viento: {viento} km/h")

    hora = ahora.get("time")
    if hora:
        partes.append(f"- Medido a las {hora[11:16]}, hora local del lugar")

    return "\n".join(partes)


def _nombre_completo(lugar: dict) -> str:
    """"Rosario, Provincia de Santa Fe, Argentina" — sin comas de más."""
    return ", ".join(
        p for p in (lugar.get("nombre"), lugar.get("provincia"), lugar.get("pais")) if p
    )


def _describir_cielo(codigo) -> str:
    """El código WMO en castellano. Si es uno raro, devolvemos el número."""
    if codigo is None:
        return "sin datos"
    return CIELO.get(codigo, f"sin descripción (código {codigo})")


# -- Tiendanube -----------------------------------------------------------
#
# Solo lectura, a propósito: el token que se carga acá puede tener permisos
# de escritura (depende de cómo se creó la app en el panel de partners), pero
# estas funciones nunca hacen POST/PUT/DELETE. El agente asesora y consulta,
# nunca modifica un pedido ni un producto.

TIENDANUBE_BASE = "https://api.tiendanube.com/v1"

# Tiendanube pide que el User-Agent diga quién es la app y cómo contactarla.
_USER_AGENT = "Agente (%s)" % (os.getenv("TIENDANUBE_CONTACTO", "").strip() or "sin contacto")

# Cómo se cuenta el estado de envío en criollo. Los valores de la izquierda
# son los que devuelve la API (ver la documentación de Tiendanube).
ENVIO = {
    "unpacked": "todavía estamos preparando tu pedido",
    "partially_packed": "estamos empaquetando tu pedido (una parte ya está lista)",
    "unshipped": "tu pedido ya está empaquetado, pronto lo enviamos",
    "partially_fulfilled": "una parte de tu pedido ya salió, el resto sigue en preparación",
    "shipped": "tu pedido ya está en camino",
    "delivered": "tu pedido ya fue entregado",
}

PAGO = {
    "paid": "está paga",
    "pending": "está pendiente de pago",
    "voided": "fue anulada",
    "refunded": "fue reembolsada",
    "partially_refunded": "fue reembolsada parcialmente",
    "abandoned": "quedó abandonada",
}


def _tiendanube_configurado() -> bool:
    return bool(os.getenv("TIENDANUBE_STORE_ID") and os.getenv("TIENDANUBE_ACCESS_TOKEN"))


def _tiendanube_pedir(ruta: str, parametros: dict) -> list | dict:
    """Un GET a la API de Tiendanube. `ruta` es relativa a /v1/<tienda>."""
    tienda = os.environ["TIENDANUBE_STORE_ID"]
    token = os.environ["TIENDANUBE_ACCESS_TOKEN"]
    url = f"{TIENDANUBE_BASE}/{tienda}{ruta}?{urllib.parse.urlencode(parametros)}"

    peticion = urllib.request.Request(url)
    # Tiendanube usa "Authentication", no "Authorization" — no es un typo.
    peticion.add_header("Authentication", f"bearer {token}")
    peticion.add_header("User-Agent", _USER_AGENT)

    try:
        with urllib.request.urlopen(peticion, timeout=ESPERA) as respuesta:
            return json.loads(respuesta.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # No es un error de verdad: Tiendanube contesta 404 en vez de una
            # lista vacía cuando una búsqueda (ej. ?q=un-numero-que-no-existe)
            # no encuentra nada. Se trata igual que "sin resultados".
            return []
        raise


# Cuánto tiene que parecerse una palabra de la búsqueda a una palabra del
# producto para contar como "encontrado". Se ajustó a mano probando contra
# la tienda real: "argentna" (falta una i) tiene que encontrar "argentina".
UMBRAL_DE_PARECIDO = 0.72

# Conectores que no dicen nada de qué producto se busca. Se sacan de la
# consulta antes de puntuar: se probó a mano que "camiseta de boca" sin este
# filtro perdía a la Titular 26/27 por culpa del "de" solo, que no matchea
# nada y diluye el promedio.
PALABRAS_VACIAS = {
    "de",
    "la",
    "el",
    "los",
    "las",
    "un",
    "una",
    "unos",
    "unas",
    "que",
    "con",
    "para",
    "por",
    "y",
    "o",
    "del",
    "al",
    "en",
    "me",
    "tenes",
    "tienen",
    "hay",
    "busco",
    "buscando",
    "quiero",
    "queria",
    "necesito",
    "estoy",
    "esta",
    "este",
    # Saludos y relleno de WhatsApp: "Holaaa capo, tenés alguna camiseta de boca
    # en stock?" tiene que buscar "camiseta boca", no seis palabras de las que
    # cuatro no matchean nada y bajan el puntaje de todos los Boca.
    "alguna",
    "alguno",
    "algun",
    "capo",
    "crack",
    "hermano",
    "bro",
    "che",
    "buenas",
    "buenos",
    "dias",
    "tardes",
    "noches",
    "stock",
    "disponible",
    "disponibles",
    "tienes",
    "favor",
    "gracias",
}


# Dicen "es una prenda", no cuál. Hay modelos que llevan la palabra en el nombre
# y otros que no ("Boca Alternativa 26/27"), así que si la consulta trae algo más
# específico se la ignora: con "camiseta de boca" seis Boca con stock quedaban
# afuera de la respuesta y el agente decía que de Boca no quedaba nada.
PALABRAS_GENERICAS = {"camiseta", "camisetas", "remera", "remeras", "camisa", "casaca", "prenda", "prendas"}


def _palabras_de_la_consulta(consulta: str) -> list[str]:
    """Las palabras que dicen qué se busca: sin signos pegados, saludos ni relleno."""
    palabras = []
    for palabra in _normalizar(consulta).split():
        palabra = palabra.strip(".,;:!?¿¡()\"'*_-")
        if palabra and palabra not in PALABRAS_VACIAS and not palabra.startswith("hola"):
            palabras.append(palabra)
    return [p for p in palabras if p not in PALABRAS_GENERICAS] or palabras


def _catalogo_completo() -> list[dict]:
    """Todos los productos publicados, de a 200 por pedido (el máximo de la API).

    Traerlo entero y comparar acá es más simple y más confiable que el
    buscador de Tiendanube, que no tolera errores de tipeo (se probó a mano:
    "argentna" ahí no encuentra nada). Se pagina porque la tienda tiene más
    de 100 modelos: con una sola página los de más abajo no se encontraban.
    """
    productos: list[dict] = []
    for pagina in range(1, 11):
        lote = _tiendanube_pedir("/products", {"per_page": 200, "page": pagina, "published": "true"})
        productos += lote
        if len(lote) < 200:
            break
    return productos


# -- Stock del mayorista ----------------------------------------------------
#
# El stock que se ofrece es el PROPIO del mayorista (tabla mayorista_stock, en
# la misma Postgres que usa el panel): el de la tienda no cuenta. Tiendanube
# aporta nombres, precios y links. Sin POSTGRES_DSN (desarrollo) se usa el
# stock de la tienda, como antes.

ORDEN_DE_TALLES = ["XS", "S", "M", "L", "XL", "XXL", "XXXL"]


def _stock_mayorista() -> dict[int, dict[str, int]] | None:
    """{modelo: {talle: cantidad}} del mayorista, o None si no hay base (desarrollo)."""
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        if os.getenv("MODO", "").strip().lower() == "produccion":
            # En producción el stock es solo el del mayorista: si no hay base, no se cae
            # al de la tienda online (que no es el que se vende), se avisa del problema.
            raise RuntimeError("falta POSTGRES_DSN: no se puede leer el stock del mayorista")
        return None
    import psycopg

    with psycopg.connect(dsn, connect_timeout=5) as conexion, conexion.cursor() as cursor:
        cursor.execute("SELECT producto_id, talle, cantidad FROM mayorista_stock")
        salida: dict[int, dict[str, int]] = {}
        for producto, talle, cantidad in cursor.fetchall():
            salida.setdefault(producto, {})[talle] = cantidad
    return salida


def _tiene_stock(producto: dict, stock: dict[int, dict[str, int]] | None) -> bool:
    if stock is None:
        return bool(producto.get("has_stock"))
    return sum((stock.get(producto.get("id")) or {}).values()) > 0


def _talles_del_mayorista(talles: dict[str, int]) -> list[str]:
    """Los talles con stock, en orden de talle, con cuántas unidades hay de cada uno."""

    def orden(item):
        base = item[0].split(" - ")[0].strip().upper()
        return (ORDEN_DE_TALLES.index(base) if base in ORDEN_DE_TALLES else 99, item[0])

    return [f"{talle} ({cantidad} u.)" for talle, cantidad in sorted(talles.items(), key=orden) if cantidad > 0]


def _buscar_en_catalogo(consulta: str, maximo: int = 6, stock: dict[int, dict[str, int]] | None = None) -> list[dict]:
    """Los productos del catálogo que mejor matchean la consulta, ordenados.

    Con una consulta genérica ("camiseta de boca", sin decir cuál) es normal
    que varios productos empaten en puntaje — ahí el desempate es a favor
    del que tiene stock, para no gastar uno de los pocos lugares del top en
    algo que la persona no puede comprar todavía.
    """
    catalogo = _catalogo_completo()
    frecuencia, total = _frecuencia_de_palabras(catalogo)

    puntuados = [(_puntaje(consulta, p, frecuencia, total), p) for p in catalogo]
    # Los puntajes casi iguales cuentan como empate, y el desempate es el stock.
    puntuados.sort(key=lambda par: (round(par[0], 2), _tiene_stock(par[1], stock)), reverse=True)

    return [p for puntaje, p in puntuados if puntaje >= UMBRAL_DE_PARECIDO][:maximo]


def _palabras_del_producto(producto: dict) -> list[str]:
    nombre = (producto.get("name") or {}).get("es") or ""
    etiquetas = producto.get("tags") or ""
    marca = producto.get("brand") or ""
    return _normalizar(f"{nombre} {etiquetas} {marca}").split()


def _frecuencia_de_palabras(catalogo: list[dict]) -> tuple[dict[str, int], int]:
    """En cuántos productos aparece cada palabra. Sirve para bajarle el peso
    a las genéricas (ver el comentario largo en _puntaje)."""
    frecuencia: dict[str, int] = {}
    for p in catalogo:
        for palabra in set(_palabras_del_producto(p)):
            frecuencia[palabra] = frecuencia.get(palabra, 0) + 1
    return frecuencia, len(catalogo)


def _rareza(palabra: str, frecuencia: dict[str, int], total: int) -> float:
    """1.0 = palabra rara (buena para diferenciar), cerca de 0 = casi universal."""
    if total == 0:
        return 1.0
    # No hace falta que la palabra de la consulta sea idéntica a una del
    # catálogo para contarla como "vista": alcanza con que se parezca mucho.
    veces = max(
        (freq for vocablo, freq in frecuencia.items() if difflib.SequenceMatcher(None, palabra, vocablo).ratio() > 0.85),
        default=0,
    )
    return 1 - min(veces / total, 0.9)


def _puntaje(consulta: str, producto: dict, frecuencia: dict[str, int], total: int) -> float:
    """Qué tan bien matchea la consulta con este producto, de 0 a 1.

    Compara palabra por palabra (no la frase entera contra el texto entero):
    así "argentna titular" encuentra "Argentina 2026 titular" aunque el
    nombre real tenga de por medio "2026" y "(Versión Jugador)" que la
    consulta no mencionó.

    Cada palabra de la consulta pesa según qué tan rara es en el catálogo.
    Motivo real, no teórico: "camiseta" aparece en casi la mitad de los
    nombres (unos sí, otros no — "Camiseta Boca Juniors 25 Aniversario" pero
    también "Boca Juniors - Titular 26/27", sin la palabra) y sin este ajuste
    "camiseta de boca" armaba un empate perfecto con cualquier producto que
    tuviera la palabra "Camiseta" en el nombre, tapando a la Titular 26/27
    —que sí era la respuesta correcta— fuera del top 5. Una palabra rara
    (un equipo, un año, "aniversario") tiene que pesar mucho más que una que
    está en medio catálogo.
    """
    palabras_producto = _palabras_del_producto(producto)
    palabras_consulta = _palabras_de_la_consulta(consulta)

    if not palabras_consulta or not palabras_producto:
        return 0.0

    pares = []
    for palabra in palabras_consulta:
        similitud = max(
            difflib.SequenceMatcher(None, palabra, otra).ratio()
            for otra in palabras_producto
        )
        pares.append((similitud, _rareza(palabra, frecuencia, total)))

    suma_pesos = sum(peso for _, peso in pares)
    if suma_pesos > 0:
        promedio = sum(s * peso for s, peso in pares) / suma_pesos
    else:
        # Las palabras de la consulta son todas genéricas (ninguna rareza):
        # ahí el peso no aporta nada y se cae a un promedio simple.
        promedio = sum(s for s, _ in pares) / len(pares)

    # El "mejor individual" también lleva su peso — si no, una sola palabra
    # genérica con match perfecto (como "camiseta" sola) volvería a colarse
    # por acá, que es justo el problema que este cambio soluciona.
    mejor = max(s * (0.5 + 0.5 * peso) for s, peso in pares)

    return (promedio + mejor) / 2


def _describir_producto(p: dict, stock: dict[int, dict[str, int]] | None = None) -> str:
    """Nombre, precio, link y talles con stock — o alternativas si no hay.

    Con `stock` (el del mayorista) los talles y las unidades salen de ahí; sin
    él, de Tiendanube.
    """
    nombre = (p.get("name") or {}).get("es") or "producto"
    link = p.get("canonical_url") or ""
    variantes = p.get("variants") or []

    precio = next(
        (v.get("promotional_price") or v.get("price") for v in variantes if v.get("price")),
        None,
    )

    lineas = [f"{nombre} — ${precio}" if precio else nombre]
    # Las categorías dicen a qué línea mayorista pertenece (G5, jugador/retro, shorts): de ahí sale el precio.
    categorias = [c for c in ((x.get("name") or {}).get("es", "") for x in p.get("categories") or []) if c]
    if categorias:
        lineas.append("Categorías: " + ", ".join(categorias))
    if link:
        lineas.append(f"Link: {link}")
    foto = _foto_de(p)
    if foto:
        # El canal de Chatwoot reconoce esta etiqueta y manda la imagen.
        lineas.append(f"[foto: {foto}]")

    if stock is not None and stock.get(p.get("id")) is None:
        # Está en la tienda pero no en el mayorista: no se puede prometer nada.
        lineas.append("No figura en el catálogo mayorista por ahora: lo confirma el equipo.")
        return "\n".join(lineas)

    if stock is None:
        con_stock = [_talle(v) for v in variantes if (v.get("stock") or 0) > 0]
    else:
        con_stock = _talles_del_mayorista(stock[p["id"]])
    if con_stock:
        lineas.append("Talles con stock: " + ", ".join(con_stock))
    else:
        lineas.append("Sin stock por ahora.")
        alternativas = _buscar_alternativas(p, stock=stock)
        if alternativas:
            lineas.append("Alternativas con stock: " + "; ".join(alternativas))

    return "\n".join(lineas)


def _foto_de(p: dict) -> str:
    """La primera foto del producto, o "" si no hay una que WhatsApp acepte.

    WhatsApp solo muestra como imagen un JPG o un PNG: un .webp llegaría como
    archivo suelto, así que esos no se mandan.
    """
    for imagen in sorted(p.get("images") or [], key=lambda i: i.get("position") or 0):
        url = (imagen.get("src") or "").strip()
        if url.lower().endswith((".png", ".jpg", ".jpeg")):
            return url if url.startswith("http") else "https:" + url
    return ""


def _talle(variante: dict) -> str:
    valores = variante.get("values") or []
    return ", ".join(v.get("es", "") for v in valores if v.get("es")) or "único"


def _buscar_alternativas(producto: dict, maximo: int = 3, stock: dict[int, dict[str, int]] | None = None) -> list[str]:
    """Otros productos con stock, parecidos por nombre, etiquetas o marca."""
    try:
        catalogo = _catalogo_completo()
    except Exception:
        # Una alternativa que no se pudo buscar no tiene que voltear la
        # respuesta principal: el producto original ya se informó bien.
        return []

    frecuencia, total = _frecuencia_de_palabras(catalogo)
    consulta = " ".join(_palabras_del_producto(producto))

    candidatos = [
        c
        for c in catalogo
        if c.get("id") != producto.get("id") and _tiene_stock(c, stock)
    ]
    puntuados = [
        (_puntaje(consulta, c, frecuencia, total), c) for c in candidatos
    ]
    puntuados.sort(key=lambda par: par[0], reverse=True)

    resultado = []
    for puntaje, c in puntuados:
        if puntaje < UMBRAL_DE_PARECIDO or len(resultado) >= maximo:
            break
        nombre_c = (c.get("name") or {}).get("es") or "producto"
        resultado.append(f"{nombre_c} ({c.get('canonical_url', '')})")

    return resultado


def _coincide_con_el_pedido(pedido: dict, nombre: str, correo: str) -> bool:
    """True si el correo coincide exacto, o si el nombre coincide razonablemente."""
    correo_pedido = (pedido.get("contact_email") or "").strip().lower()
    if correo_pedido and correo.strip().lower() == correo_pedido:
        return True

    nombre_pedido = _normalizar(pedido.get("contact_name") or "")
    nombre_dado = _normalizar(nombre)
    if nombre_pedido and nombre_dado and (
        nombre_dado in nombre_pedido or nombre_pedido in nombre_dado
    ):
        return True

    return False


def _normalizar(texto: str) -> str:
    """Minúsculas, sin tildes, sin espacios de más — para comparar nombres."""
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn"
    )
    return " ".join(sin_tildes.lower().split())


def _describir_pedido(pedido: dict) -> str:
    numero = pedido.get("number")

    if pedido.get("status") == "cancelled":
        return f"Pedido #{numero}: está cancelado."

    lineas = [
        f"Pedido #{numero}: {ENVIO.get(pedido.get('shipping_status'), 'sin datos de envío')}."
    ]

    pago = pedido.get("payment_status")
    if pago:
        lineas.append(f"La compra {PAGO.get(pago, pago)}.")

    seguimiento = pedido.get("shipping_tracking_number")
    if seguimiento:
        transportista = pedido.get("shipping_carrier_name")
        extra = f" ({transportista})" if transportista else ""
        lineas.append(f"Número de seguimiento: {seguimiento}{extra}")

    url_seguimiento = pedido.get("shipping_tracking_url")
    if url_seguimiento:
        lineas.append(f"Podés rastrearlo acá: {url_seguimiento}")

    opcion = pedido.get("shipping_option")
    if opcion:
        lineas.append(f"Forma de envío: {opcion}")

    productos = pedido.get("products") or []
    if productos:
        # Acá, a diferencia del catálogo, "name" ya es un string plano: es
        # una foto de cómo se llamaba el producto al momento de la compra,
        # no el recurso completo de /products.
        nombres = [p.get("name") or "producto" for p in productos]
        lineas.append("Productos: " + ", ".join(nombres))

    return "\n".join(lineas)
