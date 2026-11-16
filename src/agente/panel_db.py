"""Lo que se edita desde el panel de administración y el agente lee en vivo.

El panel del mayorista (carpeta mayorista/) guarda en la misma Postgres:

  · el prompt, con versiones (tabla agente_prompts: una sola está activa);
  · los ajustes del agente y los datos del negocio (tabla mayorista_config,
    claves "agente_ajustes" y "agente_datos").

El agente los lee acá, con una caché corta (20 segundos): un cambio en el panel
rige en menos de medio minuto, sin redesplegar. Si la base no contesta, o todavía
no hay nada guardado, rige lo de siempre: el prompts/sistema.md del repositorio y
las variables de entorno. Nada de esto puede tumbar al agente.

La primera vez que se lee y no hay ninguna versión, el agente guarda la del
repositorio como versión inicial, para que el panel tenga de dónde partir.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable

registro = logging.getLogger("agente.panel")

# Los datos del negocio que se pueden usar en el prompt como {{alias_pago}}.
# El panel (mayorista/admin.py) tiene la misma lista: hay una prueba que las compara.
VARIABLES = ("alias_pago", "horario", "costo_envio", "dias_despacho", "politica_cambios")

SIN_DEFINIR = "todavía no está definido: lo confirma el equipo"

_VARIABLE = re.compile(r"\{\{(\w+)\}\}")


def aplicar_datos(texto: str, datos: dict | None) -> str:
    """Reemplaza {{alias_pago}} y compañía por lo que cargó el negocio.

    Un dato que no se cargó no queda como «{{alias_pago}}» en el prompt (el modelo lo
    repetiría): se reemplaza por un aviso de que lo confirma el equipo.
    """

    def reemplazo(m: re.Match) -> str:
        nombre = m.group(1)
        if nombre not in VARIABLES:
            return m.group(0)
        valor = str((datos or {}).get(nombre) or "").strip()
        return valor or SIN_DEFINIR

    return _VARIABLE.sub(reemplazo, texto)


class PanelEnVivo:
    """El prompt, los ajustes y los datos del negocio, leídos de la base con una caché corta."""

    def __init__(
        self,
        dsn: str = "",
        texto_base: Callable[[], str] = lambda: "",
        segundos: float = 20.0,
        reloj: Callable[[], float] = time.monotonic,
    ) -> None:
        self.dsn = (dsn or "").strip()
        self._texto_base = texto_base
        self._segundos = segundos
        self._reloj = reloj
        self._candado = threading.Lock()
        self._leido_en: float | None = None
        self._prompt: str | None = None
        self._ajustes: dict = {}
        self._datos: dict = {}
        self._avisado = False

    @classmethod
    def desde_config(cls, config) -> "PanelEnVivo":
        """Solo en producción hay base compartida con el panel; en pruebas y en la compu, no."""
        from .prompts import leer_prompt

        dsn = config.postgres_dsn if config.modo == "produccion" else ""
        return cls(dsn, texto_base=lambda: leer_prompt(config.prompt_sistema))

    # -- Lo que se pide --

    def prompt(self) -> str | None:
        """El prompt activo, con los datos del negocio ya puestos. None: rige el del repositorio."""
        self._al_dia()
        return aplicar_datos(self._prompt, self._datos) if self._prompt else None

    def ajustes(self) -> dict:
        self._al_dia()
        return dict(self._ajustes)

    def datos(self) -> dict:
        self._al_dia()
        return dict(self._datos)

    def valores(self, config) -> dict:
        """Los ajustes que rigen ahora: lo guardado en el panel sobre lo del entorno."""
        base = {
            "activo": True,
            "mensajesPorRespuesta": config.mensajes_por_respuesta,
            "topeMensajesPorDia": config.tope_mensajes_por_dia,
            "respetarResueltas": config.respetar_resueltas,
            "soloElPrimerMensaje": config.solo_el_primer_mensaje,
            "avisosEmail": config.avisos_email,
        }
        return {**base, **{k: v for k, v in self.ajustes().items() if k in base}}

    # -- La base --

    def _al_dia(self) -> None:
        if not self.dsn:
            return
        with self._candado:
            if self._leido_en is not None and self._reloj() - self._leido_en < self._segundos:
                return
            try:
                self._leer()
            except Exception as e:  # noqa: BLE001
                # Si la base falla se sigue con lo último que se supo (o con lo del repositorio).
                if not self._avisado:
                    registro.warning("No se pudo leer lo del panel (%s): se sigue con lo último conocido.", e)
                    self._avisado = True
            else:
                self._avisado = False
            # También si falló: se reintenta en un rato, no en cada mensaje.
            self._leido_en = self._reloj()

    def _leer(self) -> None:
        import psycopg

        with psycopg.connect(self.dsn, connect_timeout=5) as conexion, conexion.cursor() as cursor:
            try:
                cursor.execute("SELECT texto FROM agente_prompts WHERE activa")
                fila = cursor.fetchone()
                if fila is None:
                    base = (self._texto_base() or "").strip()
                    if base:
                        cursor.execute(
                            "INSERT INTO agente_prompts (texto, usuario, nota, activa) "
                            "SELECT %s, 'sistema', 'Versión inicial: la del repositorio', TRUE "
                            "WHERE NOT EXISTS (SELECT 1 FROM agente_prompts)",
                            (base,),
                        )
                        cursor.execute("SELECT texto FROM agente_prompts WHERE activa")
                        fila = cursor.fetchone()
                self._prompt = fila[0] if fila else None
                cursor.execute("SELECT clave, valor FROM mayorista_config WHERE clave IN ('agente_ajustes', 'agente_datos')")
                guardado = {clave: valor for clave, valor in cursor.fetchall()}
            except psycopg.errors.UndefinedTable:
                # El panel todavía no creó sus tablas (arrancó antes el agente): rige lo de siempre.
                self._prompt, guardado = None, {}
        self._ajustes = guardado.get("agente_ajustes") if isinstance(guardado.get("agente_ajustes"), dict) else {}
        self._datos = guardado.get("agente_datos") if isinstance(guardado.get("agente_datos"), dict) else {}
