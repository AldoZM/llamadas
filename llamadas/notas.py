"""Genera las notas de Obsidian: '<reunión> - Transcripción.md' y '<reunión> - Resumen.md'."""

import re
from bisect import bisect_right
from pathlib import Path

from .config import Config
from .sesion import Sesion, hms

ICONO_PLATAFORMA = {"meet": "Google Meet", "teams": "Microsoft Teams", "zoom": "Zoom"}


def ruta_vault(sesion: Sesion, cfg: Config) -> str:
    """Ruta de la carpeta relativa al vault, para enlaces sin ambigüedad."""
    return sesion.carpeta.relative_to(cfg.vault).as_posix()


def nota_transcripcion(sesion: Sesion) -> Path:
    return sesion.carpeta / f"{sesion.nombre} - Transcripción.md"


def nota_resumen(sesion: Sesion) -> Path:
    return sesion.carpeta / f"{sesion.nombre} - Resumen.md"


def id_bloque(segundos: float) -> str:
    return "t" + hms(segundos).replace(":", "")


def linea_de_tiempo(sesion: Sesion, parrafos: list[dict]) -> list[dict]:
    """Mezcla párrafos, capturas y cambios del micrófono en orden cronológico."""
    items = [{"t": p["inicio"], "tipo": "habla", **p} for p in parrafos]
    items += [{"t": c["t"], "tipo": "captura", "archivo": c["archivo"]} for c in sesion.capturas]
    items += [{"t": e["t"], "tipo": "mic", "silenciado": e["silenciado"]}
              for e in sesion.eventos if e.get("tipo") == "mic"]
    orden = {"mic": 0, "captura": 1, "habla": 2}
    return sorted(items, key=lambda i: (i["t"], orden[i["tipo"]]))


def _frontmatter(campos: dict) -> str:
    lineas = ["---"]
    for k, v in campos.items():
        if isinstance(v, list):
            lineas.append(f"{k}:")
            lineas += [f"  - {x}" for x in v]
        else:
            lineas.append(f'{k}: "{v}"' if isinstance(v, str) else f"{k}: {v}")
    return "\n".join(lineas + ["---", ""])


def escribir_transcripcion(sesion: Sesion, parrafos: list[dict], cfg: Config) -> Path:
    base = ruta_vault(sesion, cfg)
    nombres = {"yo": cfg.nombre_yo, "otros": cfg.nombre_otros}
    cuerpo = [
        _frontmatter({
            "tipo": "transcripción",
            "titulo": sesion.titulo,
            "fecha": sesion.inicio,
            "plataforma": ICONO_PLATAFORMA.get(sesion.plataforma, sesion.plataforma),
            "duracion": hms(sesion.duracion),
            "resumen": f"[[{base}/{nota_resumen(sesion).stem}|Resumen]]",
            "tags": ["llamada", "transcripcion"],
        }),
        f"# {sesion.titulo}",
        "",
        f"![[{base}/audio.ogg]]",
        "",
        f"> **{cfg.nombre_yo}** = tu micrófono · los demás llevan el nombre que mostraba la llamada "
        f"(**{cfg.nombre_otros}** cuando no se pudo saber quién hablaba)",
        "",
    ]
    for item in linea_de_tiempo(sesion, parrafos):
        marca = hms(item["t"])
        if item["tipo"] == "habla":
            cuerpo.append(f"**[{marca}] {nombres.get(item['hablante'], item['hablante'])}:** {item['texto']} ^{id_bloque(item['t'])}")
        elif item["tipo"] == "captura":
            cuerpo.append(f"![[{base}/capturas/{item['archivo']}]]")
        elif item["silenciado"]:
            cuerpo.append(f"**[{marca}]** 🔇 *Micrófono silenciado*")
        else:
            cuerpo.append(f"**[{marca}]** 🎙️ *Micrófono activo*")
        cuerpo.append("")
    if not parrafos:
        cuerpo.append("*No se detectó habla en la grabación.*")
    ruta = nota_transcripcion(sesion)
    ruta.write_text("\n".join(cuerpo), encoding="utf-8")
    return ruta


def enlazar_marcas(texto: str, sesion: Sesion, parrafos: list[dict], cfg: Config) -> str:
    """Convierte '(00:12:30)' en enlaces al párrafo de la transcripción y
    '{{captura:00:03:45}}' en la imagen incrustada."""
    base = ruta_vault(sesion, cfg)
    nota = f"{base}/{nota_transcripcion(sesion).stem}"
    inicios = [p["inicio"] for p in parrafos]
    capturas = {Path(c["archivo"]).stem: c["archivo"] for c in sesion.capturas}

    def a_segundos(m: str) -> int:
        h, mi, s = (int(x) for x in m.split(":"))
        return h * 3600 + mi * 60 + s

    def enlace(marca: str) -> str:
        if not inicios:
            return marca
        i = max(bisect_right(inicios, a_segundos(marca) + 0.99) - 1, 0)
        return f"[[{nota}#^{id_bloque(inicios[i])}|{marca}]]"

    def grupo(m: re.Match) -> str:  # "(00:01:02)" o "(00:01:02, 00:03:04)"
        return "(" + re.sub(r"\d\d:\d\d:\d\d", lambda x: enlace(x.group(0)), m.group(1)) + ")"

    def captura(m: re.Match) -> str:
        archivo = capturas.get(m.group(1).replace(":", "-"))
        return f"![[{base}/capturas/{archivo}]]" if archivo else ""

    texto = re.sub(r"\{\{captura:(\d\d:\d\d:\d\d)\}\}", captura, texto)
    lineas = []
    for linea in texto.split("\n"):
        enlazada = re.sub(
            r"(?<!\[)[\[(](\d\d:\d\d:\d\d(?:\s*(?:,|;|y|-|–)\s*\d\d:\d\d:\d\d)*)[\])]", grupo, linea)
        if linea.lstrip().startswith("|"):  # dentro de una tabla el '|' del alias se escapa
            enlazada = re.sub(r"(\[\[[^\]|]+)\|", r"\1\\|", enlazada)
        lineas.append(enlazada)
    return "\n".join(lineas)


def escribir_resumen(sesion: Sesion, cuerpo: str, cfg: Config, modelo: str = "") -> Path:
    base = ruta_vault(sesion, cfg)
    contenido = [
        _frontmatter({
            "tipo": "resumen",
            "titulo": sesion.titulo,
            "fecha": sesion.inicio,
            "plataforma": ICONO_PLATAFORMA.get(sesion.plataforma, sesion.plataforma),
            "duracion": hms(sesion.duracion),
            "transcripcion": f"[[{base}/{nota_transcripcion(sesion).stem}|Transcripción]]",
            **({"analizado_con": modelo} if modelo else {}),
            "tags": ["llamada", "resumen"],
        }),
        f"# {sesion.titulo}",
        "",
        cuerpo.strip(),
        "",
    ]
    ruta = nota_resumen(sesion)
    ruta.write_text("\n".join(contenido), encoding="utf-8")
    return ruta
