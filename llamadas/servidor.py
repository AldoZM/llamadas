"""Servidor local que recibe eventos y capturas de la extensión del navegador.

Solo escucha en 127.0.0.1 y exige la cabecera X-Llamadas: eso fuerza una petición CORS
"preflight" que las páginas web no pueden pasar, así que solo la extensión puede controlarlo.
"""

import logging
import os
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from .config import Config
from .grabadora import SISTEMA, Grabadora, partes
from .procesar import pendientes, procesar
from .sesion import Sesion, hms

log = logging.getLogger("llamadas")
# Si el servidor se reinicia a mitad de una llamada, al volver la misma reunión continúa en la
# misma nota (con audio en partes) siempre que se reconecte dentro de este tiempo.
VENTANA_REANUDAR = 10 * 60
# Con activación por socket, minutos sin actividad antes de apagarse.
INACTIVIDAD = 15 * 60


class Evento(BaseModel):
    tipo: str  # "inicio" | "fin" | "mic"
    plataforma: str = ""
    titulo: str = ""
    url: str = ""
    silenciado: bool = False
    detalle: list[str] = []


class Captura(BaseModel):
    imagen: str  # data URL (image/jpeg)


class Estado:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.sesion: Sesion | None = None
        self.grabadora: Grabadora | None = None
        self.cola: queue.Queue[Path] = queue.Queue()
        self.ocupado = False  # procesando una reunión
        self.esperando = 0  # sesiones interrumpidas a la espera de ver si se reanudan
        self.ultima_actividad = time.monotonic()
        threading.Thread(target=self._trabajador, daemon=True, name="procesador").start()

    def _trabajador(self) -> None:
        while True:
            carpeta = self.cola.get()
            self.ocupado = True
            try:
                procesar(carpeta, self.cfg)
                log.info("✅ Notas listas en: %s", carpeta)
            except Exception:
                log.exception("Error procesando %s", carpeta)
            finally:
                self.ocupado = False
                self.ultima_actividad = time.monotonic()

    def inactivo(self, segundos: float) -> bool:
        """Sin llamada, sin nada que procesar y sin peticiones durante `segundos`."""
        return (self.sesion is None and not self.ocupado and self.cola.empty()
                and self.esperando == 0 and time.monotonic() - self.ultima_actividad > segundos)

    def _segundos_desde(self, iso: str) -> float:
        return (datetime.now() - datetime.fromisoformat(iso)).total_seconds()

    def _para_reanudar(self, url: str) -> Sesion | None:
        if not url or not self.cfg.reuniones.exists():
            return None
        for json_sesion in sorted(self.cfg.reuniones.glob("*/.datos/sesion.json"), reverse=True)[:5]:
            try:
                s = Sesion.cargar(json_sesion.parent.parent)
            except Exception:
                continue
            if (s.url == url and s.estado == "interrumpido" and s.fin
                    and self._segundos_desde(s.fin) < VENTANA_REANUDAR):
                return s
        return None

    def iniciar(self, ev: Evento) -> Sesion:
        with self.lock:
            if self.sesion:
                return self.sesion
            sesion = self._para_reanudar(ev.url)
            if sesion:
                sesion._t0 = time.monotonic() - self._segundos_desde(sesion.inicio)
                sesion.estado, sesion.fin = "grabando", None
                sesion.cambiar_mic(ev.silenciado)
                parte = len(partes(sesion.datos, SISTEMA)) + 1
                log.info("↩️  Continuando «%s» (parte %d)", sesion.titulo, parte)
            else:
                sesion = Sesion.nueva(self.cfg.reuniones, ev.plataforma, ev.titulo or "Reunión",
                                      ev.url, ev.silenciado)
                parte = 1
            grabadora = Grabadora(sesion.datos, self.cfg.frecuencia, self.cfg.grabar_mic,
                                  silenciado=lambda: sesion.mic_silenciado,
                                  t0=sesion._t0 if parte > 1 else None, parte=parte)
            try:
                grabadora.iniciar()
            except Exception as e:
                sesion.estado = "error"
                sesion.errores.append(str(e))
                sesion.guardar()
                raise
            self.sesion, self.grabadora = sesion, grabadora
            log.info("⏺️  Grabando «%s» (%s)", sesion.titulo, sesion.plataforma)
            return sesion

    def terminar(self, interrumpida: bool = False) -> Sesion | None:
        with self.lock:
            sesion, grabadora = self.sesion, self.grabadora
            if not sesion or not grabadora:
                return None
            self.sesion = self.grabadora = None
        grabadora.detener()
        sesion.errores += grabadora.errores
        sesion.terminar(sesion.tiempo())
        if interrumpida:
            sesion.estado = "interrumpido"
            sesion.guardar()
            return sesion
        duracion = sesion.duracion
        log.info("⏹️  Grabación terminada (%s, %d capturas). Procesando en segundo plano...",
                 hms(duracion), len(sesion.capturas))
        self.cola.put(sesion.carpeta)
        return sesion

    def procesar_luego(self, carpeta: Path, segundos: float) -> None:
        """Procesa la sesión interrumpida solo si nadie la reanudó en ese tiempo."""
        def revisar():
            try:
                if Sesion.cargar(carpeta).estado == "interrumpido":
                    log.info("No se reanudó «%s»: procesando", carpeta.name)
                    self.cola.put(carpeta)
            except Exception:
                log.exception("Error revisando %s", carpeta)
            finally:
                self.esperando -= 1
        self.esperando += 1
        threading.Timer(max(segundos, 1), revisar).start()


def crear_app(cfg: Config) -> FastAPI:
    app = FastAPI(title="Llamadas")
    estado = Estado(cfg)
    app.state.estado = estado

    def verificar(x_llamadas: str | None) -> None:
        if x_llamadas != "1":
            raise HTTPException(403, "Solo la extensión puede usar este servidor")
        estado.ultima_actividad = time.monotonic()

    @app.get("/estado")
    def ver_estado(x_llamadas: str | None = Header(None)):
        verificar(x_llamadas)
        s = estado.sesion
        return {
            "grabando": s is not None,
            "titulo": s.titulo if s else None,
            "plataforma": s.plataforma if s else None,
            "tiempo": hms(s.tiempo()) if s else None,
            "mic_silenciado": s.mic_silenciado if s else None,
            "capturas": len(s.capturas) if s else 0,
            "en_cola": estado.cola.qsize(),
            "intervalo_capturas": cfg.intervalo_capturas,
        }

    @app.post("/evento")
    def evento(ev: Evento, x_llamadas: str | None = Header(None)):
        verificar(x_llamadas)
        if ev.tipo == "inicio":
            try:
                estado.iniciar(ev)
            except Exception as e:
                log.exception("No se pudo iniciar la grabación")
                raise HTTPException(500, f"No se pudo iniciar la grabación: {e}")
        elif ev.tipo == "fin":
            estado.terminar()
        elif ev.tipo == "hablantes" and estado.sesion:
            estado.sesion.cambiar_hablantes(ev.detalle)
        elif ev.tipo == "mic" and estado.sesion:
            if estado.sesion.cambiar_mic(ev.silenciado):
                log.info("%s Micrófono %s", "🔇" if ev.silenciado else "🎙️ ",
                         "silenciado" if ev.silenciado else "activo")
        return ver_estado(x_llamadas)

    @app.post("/captura")
    def captura(c: Captura, x_llamadas: str | None = Header(None)):
        verificar(x_llamadas)
        s = estado.sesion
        if not s:  # p. ej. el servidor se reinició: la extensión volverá a iniciar la sesión
            return {"grabando": False, "guardada": None}
        try:
            return {"grabando": True, "guardada": s.agregar_captura(c.imagen, cfg.umbral_duplicado)}
        except Exception as e:
            raise HTTPException(400, f"Captura inválida: {e}")

    @app.on_event("startup")
    def reanudar():
        for carpeta in pendientes(cfg):
            sesion = Sesion.cargar(carpeta)
            transcurrido = estado._segundos_desde(sesion.fin or sesion.inicio)
            if transcurrido < VENTANA_REANUDAR:
                # Pudo cortarse a mitad de la llamada: se espera por si la extensión la retoma.
                sesion.estado = "interrumpido"
                sesion.fin = sesion.fin or datetime.now().isoformat(timespec="seconds")
                sesion.guardar()
                log.info("«%s» quedó a medias: se continuará si sigue la llamada", carpeta.name)
                estado.procesar_luego(carpeta, VENTANA_REANUDAR - transcurrido)
            else:
                log.info("Reanudando procesamiento pendiente: %s", carpeta.name)
                estado.cola.put(carpeta)

    @app.on_event("shutdown")
    def cerrar():
        if estado.terminar(interrumpida=True):
            log.warning("Se cerró el servidor durante una llamada: si la llamada sigue, "
                        "se continuará en la misma nota al volver a iniciarlo.")

    return app


def ejecutar(cfg: Config) -> None:
    app = crear_app(cfg)
    # Activación por socket (systemd): systemd escucha el puerto y arranca este proceso con la
    # primera conexión de la extensión. En ese modo el servidor se apaga solo cuando lleva un rato
    # sin llamadas ni nada que procesar; la siguiente llamada lo vuelve a arrancar.
    por_socket = os.environ.get("LISTEN_PID") == str(os.getpid()) and os.environ.get("LISTEN_FDS") == "1"
    if por_socket:
        config = uvicorn.Config(app, fd=3, log_level="warning")
        log.info("Servidor iniciado por systemd · notas en %s", cfg.reuniones)
    else:
        config = uvicorn.Config(app, host="127.0.0.1", port=cfg.puerto, log_level="warning")
        log.info("Servidor en http://127.0.0.1:%d · notas en %s", cfg.puerto, cfg.reuniones)
    servidor = uvicorn.Server(config)

    if por_socket:
        def vigilar_inactividad():
            while not servidor.should_exit:
                time.sleep(30)
                if app.state.estado.inactivo(INACTIVIDAD):
                    log.info("Sin actividad: el servidor se apaga hasta la próxima llamada.")
                    servidor.should_exit = True
        threading.Thread(target=vigilar_inactividad, daemon=True, name="inactividad").start()

    servidor.run()
