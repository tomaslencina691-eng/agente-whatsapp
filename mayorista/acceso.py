"""El login del panel de administración.

Hay un DUEÑO, cuyo usuario y clave de arranque salen de variables de entorno
(ADMIN_USUARIO y ADMIN_CLAVE), y pueden haber más personas con acceso, que el
dueño da de alta desde el panel (tabla mayorista_usuarios, con la clave guardada
con scrypt, nunca en claro). Al entrar, el navegador recibe una cookie firmada con
SESION_SECRETO que vence sola; en cada pedido se vuelve a mirar en la base que la
persona siga activa y qué puede hacer, así que quitarle el acceso rige al instante.

El dueño puede cambiar su clave desde el panel: desde ese momento vale la nueva y
la de ADMIN_CLAVE deja de valer. Si se pierde, ADMIN_RESTABLECER=1 (en Coolify) hace
valer de nuevo la de ADMIN_CLAVE hasta que se saque esa variable.

Sin ADMIN_USUARIO, ADMIN_CLAVE y un SESION_SECRETO largo el panel queda apagado (ver
`configurado`): es mejor que no exista a que exista sin clave.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

USUARIO = os.getenv("ADMIN_USUARIO", "").strip()
CLAVE = os.getenv("ADMIN_CLAVE", "")
SECRETO = os.getenv("SESION_SECRETO", "")
# Para recuperar el panel si el dueño perdió la clave que se cambió desde ahí.
RESTABLECER = os.getenv("ADMIN_RESTABLECER", "").strip().lower() in ("1", "true", "si", "yes")

# Qué puede hacer cada rol. Los grupos: stock (stock, pedidos, modelos de la tienda, historial),
# web (la web mayorista y los filtros), agente (el agente de WhatsApp), cuenta (la propia clave,
# lo que ve cualquiera) y administrar (personas con acceso y avisos: solo el dueño).
ROLES = {
    "duenio": ("stock", "web", "agente", "cuenta", "administrar"),
    "todo": ("stock", "web", "agente", "cuenta"),
    "stock_web": ("stock", "web", "cuenta"),
    "stock": ("stock", "cuenta"),
}
ROLES_ASIGNABLES = ("todo", "stock_web", "stock")
NOMBRES_DE_ROLES = {"duenio": "Dueño", "todo": "Todo menos administrar personas", "stock_web": "Stock, pedidos y web", "stock": "Solo stock y pedidos"}
LARGO_MINIMO_DE_CLAVE = 10

COOKIE = "mayorista_sesion"
DURACION_SEGUNDOS = 12 * 60 * 60

# Anti fuerza bruta: tantos intentos fallidos por IP en la ventana, y se bloquea.
MAXIMO_DE_FALLOS = 5
VENTANA_SEGUNDOS = 15 * 60
_fallos: dict[str, list[float]] = {}


def configurado() -> bool:
    # Un secreto corto se adivina: se exige largo.
    return bool(USUARIO and CLAVE and len(SECRETO) >= 24)


def _firmar(contenido: bytes) -> str:
    return hmac.new(SECRETO.encode(), contenido, hashlib.sha256).hexdigest()


def crear_sesion(usuario: str, ahora: float | None = None) -> str:
    cuerpo = base64.urlsafe_b64encode(
        json.dumps({"u": usuario, "e": int((ahora or time.time()) + DURACION_SEGUNDOS)}).encode()
    )
    return f"{cuerpo.decode()}.{_firmar(cuerpo)}"


def leer_sesion(valor: str | None, ahora: float | None = None) -> str | None:
    """El usuario de la cookie, o None si falta, está adulterada o venció."""
    if not valor or "." not in valor or not configurado():
        return None
    cuerpo, firma = valor.rsplit(".", 1)
    if not hmac.compare_digest(firma, _firmar(cuerpo.encode())):
        return None
    try:
        datos = json.loads(base64.urlsafe_b64decode(cuerpo.encode()))
    except ValueError:
        return None
    if datos.get("e", 0) < (ahora or time.time()) or not datos.get("u"):
        return None
    return datos["u"]


def credenciales_validas(usuario: str, clave: str) -> bool:
    """Compara las dos siempre, para que el tiempo no diga cuál estaba mal."""
    a = hmac.compare_digest(usuario.encode(), USUARIO.encode())
    b = hmac.compare_digest(clave.encode(), CLAVE.encode())
    return a and b


# -- Claves guardadas ----------------------------------------------------------


def hashear(clave: str) -> str:
    """La clave lista para guardar: scrypt con sal propia. Nunca se guarda la clave en claro."""
    sal = secrets.token_bytes(16)
    h = hashlib.scrypt(clave.encode(), salt=sal, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${base64.b64encode(sal).decode()}${base64.b64encode(h).decode()}"


def verificar(clave: str, guardada: str) -> bool:
    try:
        tipo, sal, h = guardada.split("$")
        if tipo != "scrypt":
            return False
        esperado = base64.b64decode(h)
        calculado = hashlib.scrypt(clave.encode(), salt=base64.b64decode(sal), n=2**14, r=8, p=1, dklen=len(esperado))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(calculado, esperado)


# Para que entrar con un usuario que no existe tarde lo mismo que con uno que existe.
_FALSA = hashear("clave-que-no-es-de-nadie")


def normalizar_usuario(usuario: str) -> str:
    return " ".join(usuario.split()).lower()


def problema_de_clave(clave: str, usuario: str = "") -> str:
    """Por qué una clave nueva no sirve (vacío si sirve)."""
    if len(clave) < LARGO_MINIMO_DE_CLAVE:
        return f"La clave tiene que tener al menos {LARGO_MINIMO_DE_CLAVE} caracteres."
    if len(clave) > 200:
        return "La clave es demasiado larga."
    if usuario and clave.lower() == usuario.lower():
        return "La clave no puede ser igual al usuario."
    return ""


def autenticar(almacen_, usuario: str, clave: str) -> dict | None:
    """Quién es la persona si usuario y clave son correctos: {"usuario", "rol"}; si no, None.

    El dueño entra con ADMIN_CLAVE, salvo que haya cambiado la clave desde el panel (entonces
    vale la nueva). Las demás personas, con la clave que les puso el dueño.
    """
    usuario = usuario.strip()
    if hmac.compare_digest(usuario.encode(), USUARIO.encode()):
        try:
            guardada = None if RESTABLECER or almacen_ is None else almacen_.usuario(normalizar_usuario(USUARIO))
        except Exception:  # noqa: BLE001
            # Si la base falla, el dueño igual tiene que poder entrar con la clave de arranque.
            guardada = None
        if guardada and guardada.get("clave_hash"):
            ok = verificar(clave, guardada["clave_hash"])
        else:
            ok = hmac.compare_digest(clave.encode(), CLAVE.encode())
        return {"usuario": USUARIO, "rol": "duenio"} if ok else None
    try:
        fila = almacen_.usuario(normalizar_usuario(usuario)) if almacen_ is not None else None
    except Exception:  # noqa: BLE001
        fila = None  # sin base no se puede validar a nadie más que al dueño
    ok = verificar(clave, fila["clave_hash"] if fila else _FALSA)
    if fila and ok and fila["activo"] and fila["rol"] in ROLES_ASIGNABLES:
        return {"usuario": fila["usuario"], "rol": fila["rol"]}
    return None


def identidad(almacen_, usuario: str | None) -> dict | None:
    """Quién es la persona de una sesión y qué puede hacer, mirando la base en este momento."""
    if not usuario:
        return None
    if usuario == USUARIO:
        return {"usuario": USUARIO, "rol": "duenio", "permisos": list(ROLES["duenio"])}
    fila = almacen_.usuario(normalizar_usuario(usuario)) if almacen_ is not None else None
    if not fila or not fila["activo"] or fila["rol"] not in ROLES_ASIGNABLES:
        return None
    return {"usuario": fila["usuario"], "rol": fila["rol"], "permisos": list(ROLES[fila["rol"]])}


def grupo_de(ruta: str) -> str:
    """Qué permiso pide cada parte del panel."""
    r = ruta.removeprefix("/admin/api")
    if r.startswith(("/usuarios", "/avisos", "/stock-compartido")):
        return "administrar"
    if r.startswith("/cuenta"):
        return "cuenta"
    if r.startswith(("/web", "/tienda/categorias")):
        return "web"
    if r.startswith("/agente"):
        return "agente"
    return "stock"


# -- Bloqueo por intentos ------------------------------------------------------


def bloqueado(ip: str, ahora: float | None = None) -> bool:
    ahora = ahora or time.time()
    recientes = [t for t in _fallos.get(ip, []) if ahora - t < VENTANA_SEGUNDOS]
    _fallos[ip] = recientes
    return len(recientes) >= MAXIMO_DE_FALLOS


def anotar_fallo(ip: str, ahora: float | None = None) -> None:
    _fallos.setdefault(ip, []).append(ahora or time.time())


def limpiar_fallos(ip: str) -> None:
    _fallos.pop(ip, None)


def clave_nueva() -> str:
    """Para generar una ADMIN_CLAVE o un SESION_SECRETO."""
    return secrets.token_urlsafe(32)
