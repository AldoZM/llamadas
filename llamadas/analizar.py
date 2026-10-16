"""Genera '<reunión> - Resumen.md' con un agente local (AGY o Claude Code) o con la API de Claude."""

import base64
import io
import logging
import re
import shutil
import subprocess

import anthropic
from PIL import Image

from .config import RUTA_PROMPT, Config
from .notas import enlazar_marcas, escribir_resumen, linea_de_tiempo, ICONO_PLATAFORMA
from .sesion import Sesion, hms

log = logging.getLogger(__name__)
ANCHO_IMAGEN_IA = 1280


class SinCredenciales(Exception):
    pass


def _imagen_b64(ruta) -> str:
    img = Image.open(ruta).convert("RGB")
    if img.width > ANCHO_IMAGEN_IA:
        img = img.resize((ANCHO_IMAGEN_IA, round(img.height * ANCHO_IMAGEN_IA / img.width)))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return base64.standard_b64encode(buf.getvalue()).decode()


def _elegir_capturas(capturas: list[dict], maximo: int) -> set[str]:
    """Si hay demasiadas, elige `maximo` repartidas uniformemente en el tiempo."""
    if len(capturas) <= maximo:
        return {c["archivo"] for c in capturas}
    paso = len(capturas) / maximo
    return {capturas[int(i * paso)]["archivo"] for i in range(maximo)}


def _contenido(sesion: Sesion, parrafos: list[dict], cfg: Config,
               imagenes_en_linea: bool = True) -> list[dict]:
    """Transcripción con las capturas intercaladas. Para la API las imágenes van incrustadas;
    para un agente CLI solo se indica la ruta del archivo y él la abre."""
    nombres = {"yo": cfg.nombre_yo, "otros": cfg.nombre_otros}
    elegidas = _elegir_capturas(sesion.capturas, cfg.max_imagenes)
    bloques: list[dict] = []
    texto = [
        f"Reunión: {sesion.titulo}",
        f"Plataforma: {ICONO_PLATAFORMA.get(sesion.plataforma, sesion.plataforma)}",
        f"Fecha: {sesion.inicio} · Duración: {hms(sesion.duracion)}",
        f'Hablantes: "{cfg.nombre_yo}" = dueño de las notas; los demás aparecen con el nombre que '
        f'mostraba la llamada (detectado por el indicador de voz, puede equivocarse en cambios rápidos '
        f'de turno); "{cfg.nombre_otros}" = no se pudo saber quién hablaba.',
        "",
        "<transcripcion>",
    ]
    for item in linea_de_tiempo(sesion, parrafos):
        marca = hms(item["t"])
        if item["tipo"] == "habla":
            texto.append(f"[{marca}] {nombres.get(item['hablante'], item['hablante'])}: {item['texto']}")
        elif item["tipo"] == "mic":
            texto.append(f"[{marca}] (micrófono de {cfg.nombre_yo} "
                         f"{'silenciado' if item['silenciado'] else 'activado'})")
        elif item["archivo"] in elegidas and not imagenes_en_linea:
            texto.append(f"[Captura {marca}: capturas/{item['archivo']}]")
        elif item["archivo"] in elegidas:
            texto.append(f"[Captura {marca}]")
            bloques.append({"type": "text", "text": "\n".join(texto)})
            bloques.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg",
                "data": _imagen_b64(sesion.carpeta / "capturas" / item["archivo"])}})
            texto = []
    texto.append("</transcripcion>")
    bloques.append({"type": "text", "text": "\n".join(texto)})
    return bloques


def analizar(sesion: Sesion, parrafos: list[dict], cfg: Config) -> None:
    if cfg.motor in MOTORES_CLI:
        cuerpo = _analizar_con_cli(sesion, parrafos, cfg)
        escribir_resumen(sesion, enlazar_marcas(cuerpo, sesion, parrafos, cfg), cfg, cfg.motor)
        log.info("Resumen listo (%s)", cfg.motor)
        return
    _analizar_con_api(sesion, parrafos, cfg)


INSTRUCCION_CLI = """Analiza la reunión que está en el archivo {entrada} (en esta carpeta).
Las líneas "[Captura HH:MM:SS: capturas/...]" indican capturas de pantalla: ábrelas y míralas \
como parte de la reunión. No modifiques ni crees archivos.

Responde ÚNICAMENTE con el Markdown final del resumen (sin preámbulo, sin explicar lo que \
hiciste y sin enlaces file://), siguiendo estas instrucciones:

{prompt}"""

# Cada motor corre en modo de solo lectura (plan): puede leer archivos, no modificarlos.
MOTORES_CLI = {
    "agy": lambda instruccion, carpeta, cfg: [
        "agy", "-p", instruccion, "--mode", "plan", "--add-dir", str(carpeta),
        *(["--model", cfg.modelo_cli] if cfg.modelo_cli else []),
        *(["--effort", cfg.esfuerzo] if cfg.esfuerzo else [])],
    "claude": lambda instruccion, carpeta, cfg: [
        "claude", "-p", instruccion, "--permission-mode", "plan", "--add-dir", str(carpeta),
        *(["--model", cfg.modelo_cli] if cfg.modelo_cli else [])],
}


def _analizar_con_cli(sesion: Sesion, parrafos: list[dict], cfg: Config) -> str:
    if not shutil.which(cfg.motor):
        raise SinCredenciales(f"No se encontró el comando `{cfg.motor}` en el PATH.")
    entrada = sesion.datos / "entrada_ia.md"
    texto = _contenido(sesion, parrafos, cfg, imagenes_en_linea=False)[0]["text"]
    entrada.write_text(texto, encoding="utf-8")
    instruccion = INSTRUCCION_CLI.format(entrada=entrada.relative_to(sesion.carpeta).as_posix(),
                                         prompt=RUTA_PROMPT.read_text(encoding="utf-8"))
    log.info("Analizando con %s...", cfg.motor)
    r = subprocess.run(MOTORES_CLI[cfg.motor](instruccion, sesion.carpeta, cfg), cwd=sesion.carpeta,
                       capture_output=True, text=True, timeout=cfg.limite_minutos * 60,
                       stdin=subprocess.DEVNULL)
    salida = r.stdout.strip()
    if r.returncode != 0 or not salida:
        raise RuntimeError(f"`{cfg.motor}` terminó con código {r.returncode}: "
                           f"{(r.stderr or salida).strip()[-500:]}")
    # Algunos agentes convierten referencias a archivos en enlaces file://: dejar solo el texto.
    return re.sub(r"\[([^\]]*)\]\(file://[^)]*\)", r"\1", salida)


def _analizar_con_api(sesion: Sesion, parrafos: list[dict], cfg: Config) -> None:
    client = anthropic.Anthropic()
    try:
        with client.beta.messages.stream(
            model=cfg.modelo_claude,
            max_tokens=64000,
            thinking={"type": "adaptive"},
            output_config={"effort": cfg.esfuerzo},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=RUTA_PROMPT.read_text(encoding="utf-8"),
            messages=[{"role": "user", "content": _contenido(sesion, parrafos, cfg)}],
        ) as stream:
            respuesta = stream.get_final_message()
    except TypeError as e:  # el SDK no encontró API key ni perfil de `ant auth login`
        if "authentication" not in str(e):
            raise
        raise SinCredenciales(str(e)) from e
    except anthropic.AuthenticationError as e:
        raise SinCredenciales(str(e)) from e

    if respuesta.stop_reason == "refusal":
        raise RuntimeError(f"La IA rechazó el análisis: {respuesta.stop_details}")
    cuerpo = "".join(b.text for b in respuesta.content if b.type == "text")
    if respuesta.stop_reason == "max_tokens":
        cuerpo += "\n\n> ⚠️ El resumen se cortó por longitud. Vuelve a ejecutar `llamada analizar`."
    escribir_resumen(sesion, enlazar_marcas(cuerpo, sesion, parrafos, cfg), cfg, respuesta.model)
    log.info("Resumen listo (%s tokens de entrada, %s de salida)",
             respuesta.usage.input_tokens, respuesta.usage.output_tokens)
