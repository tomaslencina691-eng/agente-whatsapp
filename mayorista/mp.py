"""El cliente de Mercado Pago: solo lee lo que entra a la cuenta.

Se usa para detectar que una transferencia de un mayorista llegó (ver pagos.py).
Solo hace pedidos GET: busca los cobros recientes de la cuenta. No crea cobros,
no devuelve plata ni toca nada.

El token (MERCADOPAGO_ACCESS_TOKEN) vive solo en las variables del servidor.
Mercado Pago no tiene credenciales «de solo lectura»: el token de producción de
una aplicación puede más de lo que acá se usa. Por eso conviene crear una
aplicación aparte para esto, y renovar el token si alguna vez se filtra.

Habla la API con `urllib`, como el resto del proyecto.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

log = logging.getLogger("mayorista.mp")

URL = "https://api.mercadopago.com"
TOKEN = os.getenv("MERCADOPAGO_ACCESS_TOKEN", "").strip()


class ErrorDeMercadoPago(Exception):
    """Mercado Pago contestó algo que no esperábamos. El mensaje es para mostrar."""


def configurado() -> bool:
    return bool(TOKEN)


def pedir(ruta: str, parametros: dict | None = None, reintentos: int = 2):
    """Un GET a la API. Reintenta si Mercado Pago pide esperar (429) o falla un momento."""
    if not configurado():
        raise ErrorDeMercadoPago("Falta MERCADOPAGO_ACCESS_TOKEN en el servidor.")
    consulta = "?" + urllib.parse.urlencode(parametros) if parametros else ""
    for intento in range(reintentos + 1):
        pedido = urllib.request.Request(
            f"{URL}{ruta}{consulta}",
            method="GET",
            headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/json", "User-Agent": "Mayorista-verificacion-de-pagos"},
        )
        try:
            with urllib.request.urlopen(pedido, timeout=30) as respuesta:
                return json.loads(respuesta.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            detalle = e.read().decode("utf-8", "replace")[:300]
            if e.code in (429, 500, 502, 503) and intento < reintentos:
                time.sleep(2 * (intento + 1))
                continue
            if e.code in (401, 403):
                raise ErrorDeMercadoPago(f"Mercado Pago no aceptó el token ({e.code}). Revisá que sea el token de producción de la aplicación.") from None
            raise ErrorDeMercadoPago(f"Mercado Pago devolvió {e.code} en {ruta}: {detalle}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if intento < reintentos:
                time.sleep(1 + intento)
                continue
            raise ErrorDeMercadoPago(f"No se pudo hablar con Mercado Pago: {e}") from None
    raise ErrorDeMercadoPago("Mercado Pago no contestó.")  # pragma: no cover


def cuenta() -> dict:
    """De quién es el token: {id, nombre, email}."""
    d = pedir("/users/me")
    nombre = " ".join(x for x in (d.get("first_name"), d.get("last_name")) if x) or d.get("nickname") or ""
    return {"id": d.get("id"), "nombre": nombre, "email": d.get("email") or ""}


def _fecha(texto: str | None) -> str:
    """La fecha de Mercado Pago ("2026-11-17T09:10:11.000-03:00") como ISO en UTC."""
    if not texto:
        return datetime.now(timezone.utc).isoformat()
    try:
        return datetime.fromisoformat(texto.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except ValueError:
        return datetime.now(timezone.utc).isoformat()


def normalizar(p: dict, mi_id=None) -> dict | None:
    """Un cobro de Mercado Pago como lo usa el mayorista, o None si no es plata que entró a la cuenta."""
    if p.get("status") != "approved":
        return None
    try:
        monto = float(p.get("transaction_amount") or 0)
    except (TypeError, ValueError):
        return None
    if monto <= 0:
        return None
    if mi_id is not None and p.get("collector_id") not in (None, mi_id):
        return None  # una compra que hizo la cuenta, no un cobro
    pagador = p.get("payer") or {}
    ident = pagador.get("identification") or {}
    nombre = " ".join(x for x in (pagador.get("first_name"), pagador.get("last_name")) if x)
    return {
        "id": str(p["id"]),
        "monto": round(monto, 2),
        "fecha": _fecha(p.get("date_approved") or p.get("date_created")),
        "pagador": {"nombre": nombre, "dni": str(ident.get("number") or ""), "email": pagador.get("email") or ""},
        "tipo": p.get("payment_type_id") or "",
        "operacion": p.get("operation_type") or "",
        "descripcion": (p.get("description") or "")[:140],
    }


def cobros_recientes(dias: int = 3, limite: int = 100, mi_id=None) -> list[dict]:
    """Lo que entró a la cuenta en los últimos días (aprobado), del más nuevo al más viejo."""
    ahora = datetime.now(timezone.utc)
    d = pedir(
        "/v1/payments/search",
        {
            "sort": "date_created", "criteria": "desc", "limit": limite, "offset": 0,
            "range": "date_created",
            "begin_date": (ahora - timedelta(days=dias)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "end_date": ahora.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        },
    )
    return [n for p in d.get("results") or [] if (n := normalizar(p, mi_id))]
