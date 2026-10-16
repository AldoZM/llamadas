import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
RUTA_CONFIG = Path(os.environ.get("LLAMADAS_CONFIG", RAIZ / "config.toml"))
RUTA_PROMPT = RAIZ / "prompt_analisis.md"


@dataclass
class Config:
    vault: Path
    carpeta: str
    puerto: int
    grabar_mic: bool
    frecuencia: int
    borrar_wav: bool
    intervalo_capturas: int
    umbral_duplicado: int
    modelo_whisper: str
    idioma: str | None
    nombre_yo: str
    nombre_otros: str
    analisis_activo: bool
    motor: str
    modelo_cli: str
    limite_minutos: int
    modelo_claude: str
    esfuerzo: str
    max_imagenes: int

    @property
    def reuniones(self) -> Path:
        return self.vault / self.carpeta


def cargar() -> Config:
    datos = tomllib.loads(RUTA_CONFIG.read_text(encoding="utf-8")) if RUTA_CONFIG.exists() else {}
    audio = datos.get("audio", {})
    capturas = datos.get("capturas", {})
    trans = datos.get("transcripcion", {})
    analisis = datos.get("analisis", {})
    return Config(
        vault=Path(datos.get("vault", Path.home() / "Documents" / "Llamadas")).expanduser(),
        carpeta=datos.get("carpeta", "Reuniones"),
        puerto=int(datos.get("puerto", 8765)),
        grabar_mic=bool(audio.get("grabar_mic", True)),
        frecuencia=int(audio.get("frecuencia", 48000)),
        borrar_wav=bool(audio.get("borrar_wav", True)),
        intervalo_capturas=int(capturas.get("intervalo", 15)),
        umbral_duplicado=int(capturas.get("umbral_duplicado", 6)),
        modelo_whisper=trans.get("modelo", "large-v3-turbo"),
        idioma=trans.get("idioma") or None,
        nombre_yo=trans.get("nombre_yo", "Yo"),
        nombre_otros=trans.get("nombre_otros", "Otros"),
        analisis_activo=bool(analisis.get("activo", True)),
        motor=analisis.get("motor", "agy"),
        modelo_cli=analisis.get("modelo_cli", ""),
        limite_minutos=int(analisis.get("limite_minutos", 30)),
        modelo_claude=analisis.get("modelo", "claude-opus-5-5"),
        esfuerzo=analisis.get("esfuerzo", "high"),
        max_imagenes=int(analisis.get("max_imagenes", 60)),
    )
