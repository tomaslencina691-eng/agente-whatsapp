"""Los ajustes de la web mayorista que se editan desde el panel.

Lo que no se tocó viene de `negocio.json` y de las variables de entorno; lo que
se guarda desde el panel (en la base, tabla mayorista_config, clave "web") lo
reemplaza. Así el negocio puede cambiar el número de WhatsApp, los precios, los
textos o el color sin redesplegar, y un servidor nuevo arranca con los valores
de siempre.

El logo se guarda aparte (clave "logo"): es una imagen y no tiene que viajar
en cada lectura de los ajustes.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re

from pydantic import BaseModel, Field

# El mensaje que recibe el equipo por WhatsApp. Una línea con datos que quedaron
# vacíos (el nombre, las notas, el precio si está oculto) no se manda.
MENSAJE_POR_DEFECTO = """*PEDIDO MAYORISTA - {marca}*
Pedido: {codigo}
Nombre: {nombre}
Negocio / ciudad: {negocio}

{pedido}

{envio}

Total: {unidades}
Precio por unidad: {precio}
Total: {total}

Notas: {notas}

{condiciones}"""

DATOS_DEL_MENSAJE = ("marca", "codigo", "nombre", "negocio", "pedido", "envio", "unidades", "precio", "total", "notas", "condiciones")

PAGO_POR_DEFECTO = (
    "Pagás por transferencia. El stock se descuenta recién cuando el equipo verifica el pago, así que apenas envíes el pedido "
    "mandá el comprobante por el mismo chat de WhatsApp, a continuación."
)

BAJADA_POR_DEFECTO = "Camisetas importadas de clubes y selecciones. Elegí los modelos y los talles, armá tu pedido y mandalo por WhatsApp. Lo que ves es lo que llega."

ORDENES = ("novedades", "stock", "nombre")
PESO_MAXIMO_DEL_LOGO = 1024 * 1024
TIPOS_DE_LOGO = {b"\xff\xd8\xff": "image/jpeg", b"\x89PNG\r\n\x1a\n": "image/png"}


class Tramo(BaseModel):
    desde: int = Field(ge=1, le=9999)
    precio: int = Field(ge=1, le=100_000_000)


class Linea(BaseModel):
    """Un grupo de categorías con su propio mínimo y su propia escala de precios (ver precios.py)."""

    id: str = Field(pattern=r"^[a-z0-9_-]{1,20}$")
    nombre: str = Field(min_length=1, max_length=60)
    cats: list[str] = Field(default=[], max_length=12)
    minimo: int = Field(ge=1, le=9999)
    tramos: list[Tramo] = Field(default=[], max_length=6)
    # Si no está vacío, esta línea es la variante «con parche y número» de la línea con ese id:
    # no tiene categorías propias; el cliente la elige camiseta por camiseta en el carrito.
    personalizadaDe: str = Field(default="", max_length=20)


class Regla(BaseModel):
    etiqueta: str = Field(default="", max_length=30)
    titulo: str = Field(default="", max_length=60)
    texto: str = Field(default="", max_length=260)


class AjustesWeb(BaseModel):
    """Todo lo que se edita de la web. El panel manda el conjunto completo en cada guardado."""

    marca: str = Field(max_length=60)
    bandaIzq: str = Field(default="", max_length=60)
    bandaDer: str = Field(default="", max_length=60)
    titulo: str = Field(min_length=1, max_length=40)
    bajada: str = Field(default="", max_length=200)
    color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    whatsapp: str = Field(pattern=r"^\d{10,15}$")
    # Un segundo número guardado para intercambiarlo con el activo desde el panel (puede quedar vacío).
    whatsappAlterno: str = Field(default="", pattern=r"^(\d{10,15})?$")
    # La web pide los datos de envío y despacho antes de poder enviar el pedido.
    pedirEnvio: bool = True
    # Cómo se paga (alias, cuándo se descuenta el stock, que hay que mandar el comprobante): se muestra en el carrito.
    pago: str = Field(default="", max_length=600)
    # Los datos para transferir: se muestran en el carrito, con un botón para copiar el alias.
    aliasPago: str = Field(default="", max_length=60)
    titularPago: str = Field(default="", max_length=80)
    cuentaPago: str = Field(default="", max_length=60)
    mensaje: str = Field(min_length=1, max_length=1500)
    minimo: int = Field(ge=1, le=9999)
    tramos: list[Tramo] = Field(default=[], max_length=10)
    lineas: list[Linea] = Field(default=[], max_length=8)
    # Pedido parejo: un mismo talle no puede ser más que este % de su línea (0 = sin tope). Ver precios.py.
    topeTalle: int = Field(default=0, ge=0, le=100)
    # Las metas de la barra de cada línea después de su mejor precio (pedidos grandes: 50, 100, 200).
    hitos: list[int] = Field(default=[], max_length=8)
    pausa: bool = False
    aviso: str = Field(default="", max_length=160)
    limitarStock: bool = True
    filtroVersion: bool = True
    mostrarUnidades: bool = True
    ocultarPrecios: bool = False
    orden: str = "novedades"
    reglas: list[Regla] = Field(default=[], max_length=6)
    condiciones: str = Field(default="", max_length=500)
    cierre: str = Field(default="", max_length=240)
    pie: str = Field(default="", max_length=120)
    horario: str = Field(default="", max_length=120)
    instagram: str = Field(default="", max_length=200)
    tiendaUrl: str = Field(default="", max_length=200)
    seoTitulo: str = Field(default="", max_length=70)
    seoDescripcion: str = Field(default="", max_length=200)


def problemas(a: AjustesWeb) -> list[str]:
    """Lo que está mal en un conjunto de ajustes que pasó la validación de formato (vacío si está bien)."""
    malos: list[str] = []
    if a.orden not in ORDENES:
        malos.append("El orden del catálogo no es válido.")
    if "{pedido}" not in a.mensaje:
        malos.append("El mensaje del pedido tiene que incluir {pedido}: es la lista de modelos y talles.")
    desconocidos = sorted(set(re.findall(r"\{([^{}\n]*)\}", a.mensaje)) - set(DATOS_DEL_MENSAJE))
    if desconocidos:
        malos.append("El mensaje usa datos que no existen: " + ", ".join("{" + d + "}" for d in desconocidos) + ".")
    if len({t.desde for t in a.tramos}) != len(a.tramos):
        malos.append("Hay dos escalas de precio que empiezan en la misma cantidad.")
    if len({l.id for l in a.lineas}) != len(a.lineas):
        malos.append("Hay dos líneas de precios con el mismo identificador.")
    for l in a.lineas:
        if len({t.desde for t in l.tramos}) != len(l.tramos):
            malos.append(f"La línea «{l.nombre}» tiene dos escalas que empiezan en la misma cantidad.")
    ids = {l.id for l in a.lineas}
    for l in a.lineas:
        if l.personalizadaDe and (l.personalizadaDe not in ids or l.personalizadaDe == l.id):
            malos.append(f"La línea «{l.nombre}» es la variante de una línea que no existe.")
        elif l.personalizadaDe and next(o for o in a.lineas if o.id == l.personalizadaDe).personalizadaDe:
            malos.append(f"La línea «{l.nombre}» no puede ser variante de otra variante.")
        if l.personalizadaDe and l.cats:
            malos.append(f"La línea «{l.nombre}» es una variante: no lleva categorías propias.")
    if a.topeTalle and a.topeTalle < 20:
        malos.append("El tope por talle no puede ser menor que 20 %: con 5 talles, ni un pedido repartido parejo entraría.")
    if any(h < 1 or h > 100_000 for h in a.hitos) or len(set(a.hitos)) != len(a.hitos):
        malos.append("Las metas de la barra tienen que ser cantidades distintas, de 1 a 100.000.")
    repetidas = sorted({c for i, l in enumerate(a.lineas) for c in l.cats if any(c in o.cats for o in a.lineas[:i])})
    if repetidas:
        malos.append("Una categoría no puede estar en dos líneas de precios: " + ", ".join(repetidas) + ".")
    for campo, valor in (("El Instagram", a.instagram), ("La tienda", a.tiendaUrl)):
        if valor and not re.match(r"^https?://\S+$", valor):
            malos.append(f"{campo} tiene que ser un enlace que empiece con http:// o https://.")
    if not a.marca.strip():
        malos.append("Falta el nombre del negocio.")
    return malos


def por_defecto(negocio: dict, marca: str, whatsapp: str, tienda_url: str, limitar_stock: bool, minimo: int, tramos: list[dict]) -> dict:
    """Los ajustes de siempre: negocio.json y las variables de entorno."""
    return {
        "marca": marca,
        "bandaIzq": "Solo mayoristas",
        "bandaDer": "El pedido sale por WhatsApp",
        "titulo": "Camisetas por mayor.",
        "bajada": BAJADA_POR_DEFECTO,
        "color": "#1fd65f",
        "whatsapp": whatsapp,
        "whatsappAlterno": re.sub(r"\D", "", str(negocio.get("whatsappAlterno") or "")),
        "pedirEnvio": negocio.get("pedirEnvio", True) is not False,
        "pago": negocio.get("pago") or PAGO_POR_DEFECTO,
        "aliasPago": str(negocio.get("aliasPago") or ""),
        "titularPago": str(negocio.get("titularPago") or ""),
        "cuentaPago": str(negocio.get("cuentaPago") or ""),
        "mensaje": MENSAJE_POR_DEFECTO,
        "minimo": minimo,
        "tramos": sorted(({"desde": t["desde"], "precio": t["precio"]} for t in tramos), key=lambda t: t["desde"]),
        "lineas": [
            {
                "id": l["id"],
                "nombre": l["nombre"],
                "cats": list(l.get("cats") or []),
                "personalizadaDe": l.get("personalizadaDe") or "",
                "minimo": int(l.get("minimo", minimo)),
                "tramos": sorted(({"desde": int(t["desde"]), "precio": int(t["precio"])} for t in l.get("tramos") or []), key=lambda t: t["desde"]),
            }
            for l in negocio.get("lineas") or []
        ],
        "topeTalle": int(negocio.get("topeTalle") or 0),
        "hitos": sorted({int(h) for h in negocio.get("hitos") or [] if int(h) > 0}),
        "pausa": False,
        "aviso": "",
        "limitarStock": limitar_stock,
        "filtroVersion": negocio.get("filtroVersion", True) is not False,
        "mostrarUnidades": True,
        "ocultarPrecios": False,
        "orden": "novedades",
        "reglas": [{"etiqueta": r.get("etiqueta", ""), "titulo": r.get("titulo", ""), "texto": r.get("texto", "")} for r in negocio.get("reglas") or []],
        "condiciones": negocio.get("condiciones") or "",
        "cierre": negocio.get("cierre") or "",
        "pie": negocio.get("pie") or "",
        "horario": "",
        "instagram": "",
        "tiendaUrl": tienda_url,
        "seoTitulo": "",
        "seoDescripcion": "",
        "logoHash": "",  # lo maneja el panel al subir o quitar el logo; no es un campo del formulario
    }


def combinar(base: dict, guardado: dict | None) -> dict:
    """Lo guardado desde el panel pisa lo de siempre; lo que no se guardó queda como estaba."""
    if not isinstance(guardado, dict):
        return dict(base)
    return {**base, **{k: v for k, v in guardado.items() if k in base}}


def leer_logo(datos: str) -> tuple[bytes, str]:
    """Valida una imagen subida (con o sin el prefijo data:...) y devuelve sus bytes y su tipo."""
    crudo_b64 = datos.split(",", 1)[1] if datos.startswith("data:") else datos
    try:
        crudo = base64.b64decode(crudo_b64, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("No se pudo leer la imagen.") from None
    if len(crudo) > PESO_MAXIMO_DEL_LOGO:
        raise ValueError("El logo pesa más de 1 MB.")
    for firma, tipo in TIPOS_DE_LOGO.items():
        if crudo.startswith(firma):
            return crudo, tipo
    if crudo[:4] == b"RIFF" and crudo[8:12] == b"WEBP":
        return crudo, "image/webp"
    raise ValueError("El logo tiene que ser JPG, PNG o WebP.")


def huella(crudo: bytes) -> str:
    return hashlib.sha256(crudo).hexdigest()[:8]
