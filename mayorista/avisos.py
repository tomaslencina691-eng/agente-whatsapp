"""Los avisos por mail del panel: un pedido nuevo, un modelo que se queda sin stock...

El mayorista no manda mails por su cuenta: se los pide al agente (que ya tiene el mail
configurado, ver src/agente/avisos.py) por la puerta interna /panel/avisar. Así el correo
se configura en un solo lugar, y el destinatario es el «Mail de avisos» de la pestaña Agente.

Un aviso nunca puede estropear lo que lo origina: se manda en un hilo aparte y, si algo
falla (el agente no contesta, el mail no está configurado), queda en el registro y nada más.
El agente además agrupa los avisos repetidos: de un mismo tipo sale un mail por hora como mucho.
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("mayorista.avisos")

# Lo que se avisa si nadie lo cambió desde el panel.
DEFECTO = {"pedidoNuevo": True, "pocasUnidades": True, "umbral": 2, "modelosNuevos": True, "fallasDeLaTienda": True}
EVENTOS = ("pedidoNuevo", "pocasUnidades", "modelosNuevos", "fallasDeLaTienda")


def cruzaron_el_umbral(antes: list[dict], despues: list[dict], umbral: int) -> list[tuple[int, int]]:
    """Los modelos que tenían más unidades que el umbral y ahora tienen el umbral o menos.

    Recibe las filas de stock (id, producto_id, talle, cantidad) de antes y de después de un cambio.
    Devuelve [(modelo, unidades que le quedan)]. Avisa al cruzar, no mientras sigue bajo.
    """

    def totales(filas: list[dict]) -> dict[int, int]:
        t: dict[int, int] = {}
        for f in filas:
            t[f["producto_id"]] = t.get(f["producto_id"], 0) + f["cantidad"]
        return t

    a, d = totales(antes), totales(despues)
    return [(p, n) for p, n in d.items() if n <= umbral < a.get(p, 0)]


class Avisador:
    def __init__(self, almacen_, agente_, sincrono: bool = False, reloj=time.monotonic) -> None:
        self.almacen = almacen_
        self.agente = agente_
        self.sincrono = sincrono  # para las pruebas: sin hilos
        self._reloj = reloj
        self._leido: tuple[float, dict] | None = None

    def invalidar(self) -> None:
        self._leido = None

    def config(self) -> dict:
        """Qué se avisa: lo del panel sobre lo de siempre. Se lee de la base cada 15 segundos."""
        if self._leido and self._reloj() - self._leido[0] < 15:
            return self._leido[1]
        guardado: dict = {}
        if self.almacen is not None:
            try:
                guardado = self.almacen.config("avisos") or {}
            except Exception:  # noqa: BLE001
                log.warning("No se pudo leer la configuración de avisos: se usan los valores de siempre.")
                return self._leido[1] if self._leido else dict(DEFECTO)
        valor = {**DEFECTO, **{k: v for k, v in guardado.items() if k in DEFECTO}}
        self._leido = (self._reloj(), valor)
        return valor

    def _mandar(self, evento: str, asunto: str, cuerpo: str, tipo: str) -> None:
        if self.agente is None or not self.config().get(evento):
            return

        def tarea() -> None:
            try:
                self.agente.avisar(asunto, cuerpo, tipo)
            except Exception as e:  # noqa: BLE001
                log.warning("No se pudo mandar el aviso «%s»: %s", asunto, e)

        if self.sincrono:
            tarea()
        else:
            threading.Thread(target=tarea, daemon=True).start()

    # -- Los avisos --

    def pedido_nuevo(self, codigo: str, unidades: int, total: int | None, nombre: str, negocio: str) -> None:
        quien = ", ".join(x for x in (nombre, negocio) if x) or "alguien que no dejó su nombre"
        self._mandar(
            "pedidoNuevo",
            f"Pedido nuevo {codigo}: {unidades} {'camiseta' if unidades == 1 else 'camisetas'}",
            f"Entró un pedido por la web mayorista.\n\nCódigo: {codigo}\nDe: {quien}\nCamisetas: {unidades}"
            + (f"\nTotal estimado: ${total:,}".replace(",", ".") if total else "")
            + "\n\nEntrá al panel, pestaña Pedidos, para revisarlo y confirmarlo cuando llegue el comprobante.",
            f"pedido:{codigo}",
        )

    def pocas_unidades(self, modelos: list[tuple[str, int]]) -> None:
        if not modelos:
            return
        umbral = self.config()["umbral"]
        lineas = "\n".join(f"- {nombre}: " + ("agotado" if n == 0 else f"{n} u.") for nombre, n in modelos)
        self._mandar(
            "pocasUnidades",
            f"Quedan pocas unidades: {len(modelos)} {'modelo' if len(modelos) == 1 else 'modelos'}",
            f"Estos modelos llegaron a {umbral} unidades o menos en el mayorista:\n\n{lineas}\n\nCargá stock desde el panel, pestaña Stock.",
            "poco-stock:" + ",".join(sorted(n for n, _ in modelos))[:120],
        )

    def modelos_nuevos(self, nombres: list[str]) -> None:
        if not nombres:
            return
        lista = "\n".join(f"- {n}" for n in nombres[:30]) + (f"\n- y {len(nombres) - 30} más" if len(nombres) > 30 else "")
        self._mandar(
            "modelosNuevos",
            f"Hay {len(nombres)} {'modelo nuevo' if len(nombres) == 1 else 'modelos nuevos'} en la tienda",
            f"Subiste a la tienda y todavía no están en el mayorista:\n\n{lista}\n\nEntrá al panel, pestaña Modelos de la tienda, para incluirlos o ignorarlos.",
            "modelos-nuevos:" + str(len(nombres)),
        )

    def fallo_la_tienda(self, error: str) -> None:
        self._mandar(
            "fallasDeLaTienda",
            "El mayorista no pudo leer Tiendanube",
            f"La web mayorista no pudo leer la tienda y está mostrando los últimos datos que tenía.\n\nError: {error}\n\n"
            "Puede ser un corte de Tiendanube o una clave que venció. Si sigue así, revisá la conexión.",
            "tienda",
        )
