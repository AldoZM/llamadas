"""Graba el micrófono y el audio de salida del sistema en dos archivos mono sincronizados.

Cada canal se lee en su propio hilo y un único hilo escritor los guarda en disco. El escritor
rellena con silencio los huecos que deja el dispositivo (por ejemplo, el loopback de Windows no
entrega datos mientras nada suena), así ambos archivos quedan alineados con el reloj real.
"""

import logging
import queue
import sys
import threading
import time
import warnings
from pathlib import Path
from typing import Callable

import numpy as np
import soundcard as sc
import soundfile as sf

warnings.filterwarnings("ignore", category=RuntimeWarning, module="soundcard")
log = logging.getLogger(__name__)

MIC, SISTEMA = 0, 1
NOMBRES = {MIC: "mic.wav", SISTEMA: "sistema.wav"}


def dispositivo_salida():
    """El 'micrófono' loopback que escucha lo que suena por la salida predeterminada."""
    altavoz = sc.default_speaker()
    # PulseAudio/PipeWire: "<sink>.monitor"; Windows (WASAPI): mismo id que el altavoz.
    for mic in sc.all_microphones(include_loopback=True):
        if mic.isloopback and mic.id in (altavoz.id, f"{altavoz.id}.monitor"):
            return mic
    raise RuntimeError(f"No se encontró el loopback de la salida '{altavoz.name}'")


def dispositivo_mic():
    return sc.default_microphone()


class Grabadora:
    def __init__(self, carpeta: Path, frecuencia: int, grabar_mic: bool,
                 silenciado: Callable[[], bool], t0: float | None = None, parte: int = 1,
                 al_escribir: Callable[[int, np.ndarray], None] | None = None):
        """`t0`/`parte` permiten continuar una sesión interrumpida: la parte nueva se escribe en
        archivos aparte (mic-2.wav...), alineada con el reloj de la sesión original.
        `al_escribir(canal, audio)` recibe todo lo que se escribe (p. ej. para transcribir en vivo)."""
        self.carpeta = carpeta
        self.archivos = {c: n if parte == 1 else n.replace(".wav", f"-{parte}.wav")
                         for c, n in NOMBRES.items()}
        self._t0_fijo = t0
        self.sr = frecuencia
        self.grabar_mic = grabar_mic
        self.silenciado = silenciado
        self.al_escribir = al_escribir
        self._cola: queue.Queue = queue.Queue()
        self._parar = threading.Event()
        self._hilos: list[threading.Thread] = []
        self._escritor: threading.Thread | None = None
        self.t0 = 0.0
        self.errores: list[str] = []

    def iniciar(self) -> None:
        self.carpeta.mkdir(parents=True, exist_ok=True)
        self.t0 = self._t0_fijo if self._t0_fijo is not None else time.monotonic()
        fuentes = [(SISTEMA, dispositivo_salida())]
        if self.grabar_mic:
            fuentes.append((MIC, dispositivo_mic()))
        for canal, dispositivo in fuentes:
            log.info("Canal %s: %s", NOMBRES[canal], dispositivo.name)
            hilo = threading.Thread(target=self._leer, args=(canal, dispositivo),
                                    daemon=True, name=f"lector-{canal}")
            hilo.start()
            self._hilos.append(hilo)
        self._escritor = threading.Thread(target=self._escribir, daemon=True, name="escritor")
        self._escritor.start()

    def detener(self) -> float:
        """Detiene la grabación y devuelve la duración en segundos."""
        duracion = time.monotonic() - self.t0
        self._parar.set()
        if self._escritor:
            self._escritor.join(timeout=30)
        return duracion

    def _leer(self, canal: int, dispositivo) -> None:
        bloque = self.sr // 10
        if sys.platform == "win32":  # WASAPI necesita COM inicializado en cada hilo
            import ctypes
            ctypes.windll.ole32.CoInitializeEx(None, 0)
        try:
            with dispositivo.recorder(samplerate=self.sr, blocksize=bloque) as rec:
                while not self._parar.is_set():
                    datos = rec.record(numframes=bloque)
                    mono = datos.mean(axis=1) if datos.ndim > 1 else datos
                    if canal == MIC and self.silenciado():
                        mono = np.zeros_like(mono)
                    self._cola.put((canal, time.monotonic() - self.t0, mono.astype(np.float32)))
        except Exception as e:  # el dispositivo desapareció, permisos, etc.
            msg = f"Error grabando {NOMBRES[canal]}: {e}"
            log.error(msg)
            self.errores.append(msg)

    def _escribir(self) -> None:
        archivos = {c: sf.SoundFile(self.carpeta / n, "w", samplerate=self.sr, channels=1,
                                    subtype="PCM_16") for c, n in self.archivos.items()}
        escritos = {c: 0 for c in NOMBRES}
        tolerancia = self.sr // 2
        try:
            while not (self._parar.is_set() and self._cola.empty()):
                try:
                    canal, t_fin, datos = self._cola.get(timeout=0.2)
                except queue.Empty:
                    continue
                inicio_esperado = int(t_fin * self.sr) - len(datos)
                hueco = inicio_esperado - escritos[canal]
                if hueco > tolerancia:
                    _silencio(archivos[canal], hueco, self.sr)
                    escritos[canal] += hueco
                    self._avisar(canal, np.zeros(hueco, dtype=np.float32))
                archivos[canal].write(datos)
                escritos[canal] += len(datos)
                self._avisar(canal, datos)
            # Igualar ambos canales a la duración real de la sesión.
            total = max(int((time.monotonic() - self.t0) * self.sr), *escritos.values())
            for canal, f in archivos.items():
                if total > escritos[canal]:
                    _silencio(f, total - escritos[canal], self.sr)
        finally:
            for f in archivos.values():
                f.close()


    def _avisar(self, canal: int, datos: np.ndarray) -> None:
        if self.al_escribir:
            try:
                self.al_escribir(canal, datos)
            except Exception:  # nunca debe afectar a la grabación
                log.exception("Error pasando audio a la transcripción en vivo")
                self.al_escribir = None


def _silencio(archivo: sf.SoundFile, n: int, sr: int) -> None:
    bloque = sr * 10  # por bloques, para no reservar mucha memoria en huecos largos
    while n > 0:
        archivo.write(np.zeros(min(n, bloque), dtype=np.float32))
        n -= bloque


def partes(carpeta: Path, canal: int) -> list[Path]:
    base = NOMBRES[canal].removesuffix(".wav")
    return sorted(carpeta.glob(f"{base}*.wav"))


def mezclar_estereo(carpeta: Path, destino: Path) -> None:
    """mic*.wav (izquierda) + sistema*.wav (derecha) -> archivo estéreo comprimido.
    Si la sesión tuvo varias partes, cada una ya trae silencio donde no grababa: se suman."""
    entradas = {c: [sf.SoundFile(p) for p in partes(carpeta, c)] for c in NOMBRES}
    sr = next(f for fs in entradas.values() for f in fs).samplerate
    try:
        with sf.SoundFile(destino, "w", samplerate=sr, channels=2,
                          format="OGG", subtype="VORBIS") as salida:
            bloque = sr * 10
            while True:
                lecturas = {c: [f.read(bloque, dtype="float32") for f in fs] for c, fs in entradas.items()}
                n = max((len(a) for ls in lecturas.values() for a in ls), default=0)
                if n == 0:
                    break
                estereo = np.zeros((n, 2), dtype=np.float32)
                for col, canal in ((0, MIC), (1, SISTEMA)):
                    for a in lecturas[canal]:
                        estereo[:len(a), col] += a
                salida.write(np.clip(estereo, -1, 1))
    finally:
        for fs in entradas.values():
            for f in fs:
                f.close()
