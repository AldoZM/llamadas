"""Transcribe cada canal por separado (izquierda = yo, derecha = otros) con faster-whisper."""

import json
import logging
import os
import re
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
import soundfile as sf
from faster_whisper import WhisperModel

from .config import Config
from .sesion import Sesion

log = logging.getLogger(__name__)
SR_WHISPER = 16000
_modelo: WhisperModel | None = None
_nombre_modelo = ""


def modelo(nombre: str) -> WhisperModel:
    global _modelo, _nombre_modelo
    if _modelo is None or _nombre_modelo != nombre:
        log.info("Cargando modelo Whisper '%s' (la primera vez se descarga)...", nombre)
        _modelo = WhisperModel(nombre, device="auto", compute_type="int8",
                               cpu_threads=max(1, (os.cpu_count() or 4) // 2))  # deja la PC usable
        _nombre_modelo = nombre
    return _modelo


def leer_16k(ruta: Path) -> tuple[np.ndarray, np.ndarray]:
    """Lee el audio estéreo y lo baja a 16 kHz (lo que espera Whisper), por bloques."""
    sr = sf.info(ruta).samplerate
    # Filtro paso bajo (sinc con ventana) para evitar aliasing al reducir la frecuencia.
    corte = 7200 / sr
    n = np.arange(101) - 50
    filtro = (2 * corte * np.sinc(2 * corte * n) * np.hamming(101)).astype(np.float32)
    canales: list[list[np.ndarray]] = [[], []]
    for bloque in sf.blocks(str(ruta), blocksize=sr * 30, dtype="float32", always_2d=True):
        posiciones = np.arange(int(len(bloque) * SR_WHISPER / sr)) * sr / SR_WHISPER
        for c in range(2):
            filtrado = np.convolve(bloque[:, c], filtro, mode="same")
            canales[c].append(np.interp(posiciones, np.arange(len(bloque)), filtrado).astype(np.float32))
    return np.concatenate(canales[0]), np.concatenate(canales[1])


def _transcribir_canal(audio: np.ndarray, cfg: Config) -> list[dict]:
    if audio.size == 0 or float(np.abs(audio).max()) < 1e-4:
        return []  # canal en silencio total (mic desactivado)
    segmentos, _ = modelo(cfg.modelo_whisper).transcribe(
        audio, language=cfg.idioma, beam_size=5, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        condition_on_previous_text=False)
    return [{"inicio": round(s.start, 2), "fin": round(s.end, 2), "texto": s.text.strip()}
            for s in segmentos if s.text.strip()]


def _normalizar(texto: str) -> str:
    return re.sub(r"[^\w ]+", "", texto.lower())


def _quitar_eco(mios: list[dict], otros: list[dict]) -> list[dict]:
    """Descarta frases del mic que en realidad son la voz de los demás saliendo por bocinas."""
    limpios = []
    for m in mios:
        eco = False
        for o in otros:
            solape = min(m["fin"], o["fin"]) - max(m["inicio"], o["inicio"])
            if solape <= 0:
                continue
            if (solape / max(m["fin"] - m["inicio"], 0.1) > 0.5 and
                    SequenceMatcher(None, _normalizar(m["texto"]), _normalizar(o["texto"])).ratio() > 0.6):
                eco = True
                break
        if not eco:
            limpios.append(m)
    return limpios


def transcribir(sesion: Sesion, cfg: Config) -> list[dict]:
    """Devuelve segmentos [{inicio, fin, texto, hablante}] y los guarda en .datos/."""
    destino = sesion.datos / "transcripcion.json"
    if destino.exists():
        return json.loads(destino.read_text(encoding="utf-8"))
    izquierda, derecha = leer_16k(sesion.carpeta / "audio.ogg")
    log.info("Transcribiendo a los demás...")
    otros = _transcribir_canal(derecha, cfg)
    log.info("Transcribiendo tu micrófono...")
    mios = _quitar_eco(_transcribir_canal(izquierda, cfg), otros)
    segmentos = ([{**s, "hablante": "yo"} for s in mios] +
                 [{**s, "hablante": "otros"} for s in otros])
    segmentos.sort(key=lambda s: s["inicio"])
    destino.write_text(json.dumps(segmentos, ensure_ascii=False, indent=1), encoding="utf-8")
    return segmentos


# Textos de botones de la interfaz que a veces se leen como si fueran el nombre.
_PREFIJOS_UI = re.compile(r"^(no fijar|fijar a|dejar de fijar a|presentación de|presentation by|"
                          r"unpin|pin)\s+(la presentación de\s+)?", re.I)
_SUFIJOS_UI = re.compile(r"(\s+(a|to) (tu|your) pantalla principal|\s*\((tú|you|presentación|"
                         r"presentando|presenting)\))$", re.I)
_SOLO_UI = {"encuadrar", "ampliar imagen", "reducir imagen", "fijar", "más opciones", "more options", "quitar", "silenciar", "mute",
            "presentando", "presenting", "tú", "you"}


def limpiar_nombre(nombre: str) -> str | None:
    n = _SUFIJOS_UI.sub("", _PREFIJOS_UI.sub("", nombre.strip())).strip()
    return None if len(n) < 3 or n.lower() in _SOLO_UI else n


def asignar_hablantes(segmentos: list[dict], eventos: list[dict]) -> list[dict]:
    """Pone el nombre real a cada frase de "otros" según quién marcaba la plataforma como
    hablante durante esa frase. Si no hay datos (otra plataforma, pestaña oculta), queda "otros"."""
    cambios = [e for e in eventos if e.get("tipo") == "hablantes"]
    if not cambios:
        return segmentos
    tramos = []  # (inicio, fin, nombres)
    for i, e in enumerate(cambios):
        fin = cambios[i + 1]["t"] if i + 1 < len(cambios) else float("inf")
        nombres = list(dict.fromkeys(filter(None, map(limpiar_nombre, e["nombres"]))))
        if nombres:
            tramos.append((e["t"], fin, nombres))
    resultado = []
    for s in segmentos:
        if s["hablante"] != "otros":
            resultado.append(s)
            continue
        tiempo: dict[str, float] = {}
        # El indicador visual va ~1 s detrás del audio: se amplía un poco la ventana.
        ini, fin = s["inicio"] - 0.5, s["fin"] + 1.0
        for t0, t1, nombres in tramos:
            solape = min(fin, t1) - max(ini, t0)
            if solape > 0:
                for orden, n in enumerate(nombres):  # el primero es quien más actividad tuvo
                    tiempo[n] = tiempo.get(n, 0) + solape / (orden + 1)
        nombre = max(tiempo, key=tiempo.get) if tiempo else "otros"
        resultado.append({**s, "hablante": nombre})
    return resultado


def agrupar(segmentos: list[dict], pausa: float = 1.5, largo_max: int = 700) -> list[dict]:
    """Une frases seguidas del mismo hablante en párrafos legibles."""
    parrafos: list[dict] = []
    for s in segmentos:
        p = parrafos[-1] if parrafos else None
        if (p and p["hablante"] == s["hablante"] and s["inicio"] - p["fin"] <= pausa
                and len(p["texto"]) + len(s["texto"]) < largo_max):
            p["texto"] += " " + s["texto"]
            p["fin"] = s["fin"]
        else:
            parrafos.append(dict(s))
    return parrafos
