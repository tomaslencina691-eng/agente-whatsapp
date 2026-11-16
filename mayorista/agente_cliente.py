"""El panel hablándole al agente, por la red interna del servidor.

Dos cosas necesita el panel del agente, y las dos las tiene el agente (que es quien
conoce el modelo, la clave y las herramientas):

  · GET  /panel/estado   el modelo, el prompt del repositorio y los ajustes que rigen;
  · POST /panel/probar   una prueba suelta: contesta con el prompt que se le manda,
                         sin tocar ninguna conversación real.

Las dos puertas piden un secreto compartido (AGENTE_PANEL_TOKEN, el mismo en los dos
servicios). Sin él (o con uno de menos de 16 caracteres) quedan apagadas; con uno
equivocado, rechazan. El panel las llama por la red interna (AGENTE_URL).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


class ErrorDelAgente(Exception):
    """El agente no contestó o devolvió un error. El mensaje es para mostrar."""


class AgenteHTTP:
    def __init__(self, url: str, token: str) -> None:
        self.url = url.rstrip("/")
        self.token = token

    def _pedir(self, metodo: str, ruta: str, cuerpo: dict | None = None, espera: float = 15):
        pedido = urllib.request.Request(
            self.url + ruta,
            data=json.dumps(cuerpo).encode("utf-8") if cuerpo is not None else None,
            method=metodo,
            headers={"Content-Type": "application/json", "X-Panel-Token": self.token},
        )
        try:
            with urllib.request.urlopen(pedido, timeout=espera) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                detalle = json.loads(e.read().decode("utf-8")).get("error") or ""
            except Exception:  # noqa: BLE001
                detalle = ""
            raise ErrorDelAgente(detalle or f"El agente contestó {e.code}.") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ErrorDelAgente(f"No se pudo hablar con el agente: {e}") from None

    def estado(self) -> dict:
        return self._pedir("GET", "/panel/estado")

    def avisar(self, asunto: str, cuerpo: str, tipo: str) -> dict:
        """Un mail de aviso. El agente agrupa los repetidos (de un tipo, uno por hora como mucho)."""
        return self._pedir("POST", "/panel/avisar", {"asunto": asunto, "cuerpo": cuerpo, "tipo": tipo})

    def mail_de_prueba(self) -> dict:
        return self._pedir("POST", "/panel/mail-de-prueba", {}, espera=60)

    def probar(self, prompt: str, mensajes: list[dict], datos: dict) -> dict:
        # El modelo puede tardar y usar herramientas: se le da tiempo.
        return self._pedir("POST", "/panel/probar", {"prompt": prompt, "mensajes": mensajes, "datos": datos}, espera=120)


def crear() -> AgenteHTTP | None:
    url, token = os.getenv("AGENTE_URL", "").strip(), os.getenv("AGENTE_PANEL_TOKEN", "").strip()
    return AgenteHTTP(url, token) if url and len(token) >= 16 else None
