"""Después de la llamada: audio estéreo -> transcripción -> notas de Obsidian -> resumen con IA."""

import logging
from pathlib import Path

from . import notas
from .analizar import SinCredenciales, analizar
from .config import Config
from .grabadora import NOMBRES, mezclar_estereo, partes
from .sesion import Sesion
from .transcribir import agrupar, asignar_hablantes, transcribir

log = logging.getLogger(__name__)

RESUMEN_PENDIENTE = """> [!warning] Resumen pendiente
> {motivo}
>
> Cuando esté resuelto, ejecuta:
> `llamada analizar "{carpeta}"`
"""


def procesar(carpeta: Path, cfg: Config, solo_analizar: bool = False) -> None:
    sesion = Sesion.cargar(carpeta)
    log.info("Procesando «%s»", sesion.nombre)

    ogg = carpeta / "audio.ogg"
    if not ogg.exists():
        log.info("Creando audio.ogg...")
        mezclar_estereo(sesion.datos, ogg)
        if cfg.borrar_wav:
            for canal in NOMBRES:
                for parte in partes(sesion.datos, canal):
                    parte.unlink()

    parrafos = agrupar(asignar_hablantes(transcribir(sesion, cfg), sesion.eventos))
    if not solo_analizar:
        notas.escribir_transcripcion(sesion, parrafos, cfg)
        sesion.estado = "transcrito"
        sesion.guardar()
        log.info("Transcripción lista: %s", notas.nota_transcripcion(sesion).name)

    if not cfg.analisis_activo and not solo_analizar:
        return
    if not parrafos:
        notas.escribir_resumen(sesion, "*No se detectó habla en la grabación.*", cfg)
        return
    try:
        analizar(sesion, parrafos, cfg)
        sesion.estado = "listo"
        sesion.guardar()
    except SinCredenciales:
        motivo = "No hay credenciales de Claude: define la variable `ANTHROPIC_API_KEY`."
        log.warning(motivo)
        notas.escribir_resumen(sesion, RESUMEN_PENDIENTE.format(motivo=motivo, carpeta=carpeta), cfg)
    except Exception as e:
        log.exception("Falló el análisis")
        notas.escribir_resumen(sesion, RESUMEN_PENDIENTE.format(
            motivo=f"El análisis falló: {e}", carpeta=carpeta), cfg)


def pendientes(cfg: Config) -> list[Path]:
    """Sesiones grabadas que no se terminaron de procesar (p. ej. si se cerró el servidor)."""
    if not cfg.reuniones.exists():
        return []
    resultado = []
    for json_sesion in sorted(cfg.reuniones.glob("*/.datos/sesion.json")):
        carpeta = json_sesion.parent.parent
        try:
            if Sesion.cargar(carpeta).estado in ("grabando", "grabado", "interrumpido"):
                resultado.append(carpeta)
        except Exception:
            log.warning("Sesión ilegible: %s", carpeta)
    return resultado
