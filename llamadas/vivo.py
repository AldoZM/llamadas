"""Transcripción durante la llamada, con la CPU que quede libre.

La grabadora le pasa cada bloque de audio que escribe. Cuando se juntan unos minutos de un canal,
se corta en un silencio y se manda ese tramo a un proceso aparte con prioridad mínima (Linux:
SCHED_IDLE; Windows: IDLE_PRIORITY_CLASS), así nunca le quita CPU a la llamada. Lo transcrito se
guarda en .datos/vivo.json; al colgar solo falta transcribir el último tramo.
"""

import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np

from .config import RAIZ, Config

log = logging.getLogger(__name__)
SR = 16000
TRAMO = 3 * 60  # segundos de audio que se juntan antes de transcribir un tramo
AGRUPAR = 10  # se baja a 16 kHz en grupos de este tamaño (segundos)
CANALES = {0: "mic", 1: "sistema"}  # mismos números que grabadora.MIC / SISTEMA


def avance_en_vivo(datos: Path) -> dict:
    """{"mic"|"sistema": {"hasta": segundos ya transcritos, "segmentos": [...]}}"""
    ruta = datos / "vivo.json"
    if ruta.exists():
        try:
            return json.loads(ruta.read_text(encoding="utf-8"))
        except ValueError:
            log.warning("vivo.json ilegible: se transcribe todo de nuevo")
    return {c: {"hasta": 0.0, "segmentos": []} for c in CANALES.values()}


class TranscriptorVivo:
    def __init__(self, datos: Path, cfg: Config, sr: int):
        self.datos = datos.resolve()
        self.cfg = cfg
        self.sr = sr
        self.avance = avance_en_vivo(datos)
        self._crudo: dict[int, list[np.ndarray]] = {c: [] for c in CANALES}
        self._listo: dict[int, list[np.ndarray]] = {c: [] for c in CANALES}  # ya en 16 kHz
        self._lock = threading.Lock()
        self._parar = threading.Event()
        self._proceso: subprocess.Popen | None = None
        self._hilo: threading.Thread | None = None

    # ---- llamado desde el hilo escritor de la grabadora -------------------------------
    def alimentar(self, canal: int, datos: np.ndarray) -> None:
        crudo = self._crudo[canal]
        crudo.append(datos)
        if sum(len(d) for d in crudo) >= self.sr * AGRUPAR:
            from .transcribir import a_16k
            bloque = a_16k(np.concatenate(crudo), self.sr)
            crudo.clear()
            with self._lock:
                self._listo[canal].append(bloque)

    # ---- ciclo de vida -----------------------------------------------------------------
    def iniciar(self) -> None:
        opciones = {}
        if sys.platform == "win32":
            opciones["creationflags"] = subprocess.IDLE_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
        self._proceso = subprocess.Popen(
            [sys.executable, "-m", "llamadas", "transcriptor"], cwd=RAIZ,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", **opciones)
        self._hilo = threading.Thread(target=self._ciclo, daemon=True, name="vivo")
        self._hilo.start()

    def detener(self) -> None:
        """Al colgar: se descarta el tramo en curso; lo que falte lo transcribe el procesado final."""
        self._parar.set()
        if self._proceso:
            self._proceso.kill()
            self._proceso.wait()
        if self._hilo:
            self._hilo.join(timeout=10)
        (self.datos / "vivo-tramo.npy").unlink(missing_ok=True)

    def _ciclo(self) -> None:
        try:
            while not self._parar.wait(5):
                for canal in CANALES:
                    tramo = self._cortar(canal)
                    if tramo is not None and not self._parar.is_set():
                        self._transcribir(canal, tramo)
        except Exception:
            if not self._parar.is_set():
                log.exception("La transcripción en vivo se detuvo; se hará completa al colgar")

    def _cortar(self, canal: int) -> np.ndarray | None:
        """Saca del búfer un tramo de ~TRAMO segundos que termine en el punto más silencioso."""
        with self._lock:
            listo = self._listo[canal]
            if sum(len(b) for b in listo) < SR * TRAMO:
                return None
            audio = np.concatenate(listo)
            ventana = SR // 4
            cola = audio[-20 * SR:]
            energia = [float(np.mean(cola[i:i + ventana] ** 2))
                       for i in range(0, len(cola) - ventana + 1, ventana)]
            menor = len(energia) - 1 - int(np.argmin(energia[::-1]))  # el último, si hay empate
            corte = len(audio) - len(cola) + menor * ventana + ventana // 2
            self._listo[canal] = [audio[corte:]]
            return audio[:corte]

    def _transcribir(self, canal: int, tramo: np.ndarray) -> None:
        nombre = CANALES[canal]
        inicio = self.avance[nombre]["hasta"]
        segmentos = []
        if float(np.abs(tramo).max()) >= 1e-4:  # el mic silenciado son solo ceros
            ruta = self.datos / "vivo-tramo.npy"
            np.save(ruta, tramo)
            self._proceso.stdin.write(json.dumps({"audio": str(ruta), "inicio": inicio}) + "\n")
            self._proceso.stdin.flush()
            respuesta = self._proceso.stdout.readline()
            if not respuesta:
                raise RuntimeError("el proceso transcriptor terminó")
            segmentos = json.loads(respuesta)["segmentos"]
        self.avance[nombre]["hasta"] = round(inicio + len(tramo) / SR, 3)
        self.avance[nombre]["segmentos"] += segmentos
        (self.datos / "vivo.json").write_text(json.dumps(self.avance, ensure_ascii=False), encoding="utf-8")
        log.debug("En vivo: %s transcrito hasta %.0f s", nombre, self.avance[nombre]["hasta"])


def proceso_transcriptor(cfg: Config) -> None:
    """Proceso hijo: lee trabajos {audio, inicio} por stdin y responde {segmentos} por stdout."""
    if hasattr(os, "sched_setscheduler"):
        try:  # antes de cargar el modelo, para que sus hilos lo hereden
            os.sched_setscheduler(0, os.SCHED_IDLE, os.sched_param(0))
        except OSError:
            os.nice(19)
    salida, sys.stdout = sys.stdout, sys.stderr  # stdout queda solo para las respuestas
    from .transcribir import transcribir_canal
    hilos = max(1, (os.cpu_count() or 4) // 2)
    for linea in sys.stdin:
        trabajo = json.loads(linea)
        audio = np.load(trabajo["audio"])
        segmentos = transcribir_canal(audio, cfg, inicio=trabajo["inicio"], hilos=hilos)
        salida.write(json.dumps({"segmentos": segmentos}, ensure_ascii=False) + "\n")
        salida.flush()
