"""Estado de una reunión: carpeta en el vault, eventos (mic silenciado) y capturas de pantalla."""

import base64
import io
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import imagehash
from PIL import Image

DATOS = ".datos"  # Obsidian ignora carpetas que empiezan con punto
ANCHO_MAX_CAPTURA = 1920


def hms(segundos: float) -> str:
    s = int(segundos)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def nombre_seguro(texto: str, largo: int = 60) -> str:
    texto = re.sub(r'[\\/:*?"<>|#^\[\]]+', " ", texto)
    return re.sub(r"\s+", " ", texto).strip()[:largo].strip() or "Reunión"


@dataclass
class Sesion:
    carpeta: Path
    inicio: str
    plataforma: str
    titulo: str
    url: str = ""
    fin: str | None = None
    duracion: float = 0.0
    estado: str = "grabando"  # grabando -> grabado -> transcrito -> listo
    eventos: list[dict] = field(default_factory=list)
    capturas: list[dict] = field(default_factory=list)
    errores: list[str] = field(default_factory=list)
    mic_silenciado: bool = False
    _t0: float = field(default=0.0, repr=False)
    _ultimo_hash: object = field(default=None, repr=False)

    # ---- creación / persistencia -------------------------------------------------
    @classmethod
    def nueva(cls, reuniones: Path, plataforma: str, titulo: str, url: str,
              silenciado: bool) -> "Sesion":
        ahora = datetime.now()
        base = f"{ahora:%Y-%m-%d %H%M} {nombre_seguro(titulo)}"
        carpeta = reuniones / base
        n = 2
        while carpeta.exists():
            carpeta = reuniones / f"{base} ({n})"
            n += 1
        (carpeta / DATOS).mkdir(parents=True)
        (carpeta / "capturas").mkdir()
        s = cls(carpeta=carpeta, inicio=ahora.isoformat(timespec="seconds"),
                plataforma=plataforma, titulo=titulo, url=url, mic_silenciado=silenciado,
                _t0=time.monotonic())
        if silenciado:
            s.eventos.append({"t": 0.0, "tipo": "mic", "silenciado": True})
        s.guardar()
        return s

    @classmethod
    def cargar(cls, carpeta: Path) -> "Sesion":
        datos = json.loads((carpeta / DATOS / "sesion.json").read_text(encoding="utf-8"))
        datos["carpeta"] = carpeta
        return cls(**datos)

    def guardar(self) -> None:
        datos = {k: v for k, v in asdict(self).items()
                 if not k.startswith("_") and k != "carpeta"}
        (self.carpeta / DATOS / "sesion.json").write_text(
            json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- durante la llamada --------------------------------------------------------
    @property
    def datos(self) -> Path:
        return self.carpeta / DATOS

    @property
    def nombre(self) -> str:
        return self.carpeta.name

    def tiempo(self) -> float:
        return round(time.monotonic() - self._t0, 2)

    def cambiar_mic(self, silenciado: bool) -> bool:
        if silenciado == self.mic_silenciado:
            return False
        self.mic_silenciado = silenciado
        self.eventos.append({"t": self.tiempo(), "tipo": "mic", "silenciado": silenciado})
        self.guardar()
        return True

    def cambiar_hablantes(self, nombres: list[str]) -> None:
        """Quién aparece hablando en la llamada (según la plataforma), desde este momento."""
        self.eventos.append({"t": self.tiempo(), "tipo": "hablantes", "nombres": nombres})
        self.guardar()

    def agregar_captura(self, data_url: str, umbral: int) -> str | None:
        """Guarda la captura si es distinta de la anterior. Devuelve el nombre o None."""
        t = self.tiempo()
        crudo = base64.b64decode(data_url.split(",", 1)[-1])
        img = Image.open(io.BytesIO(crudo)).convert("RGB")
        huella = imagehash.phash(img)
        if umbral and self._ultimo_hash is not None and huella - self._ultimo_hash < umbral:
            return None
        self._ultimo_hash = huella
        if img.width > ANCHO_MAX_CAPTURA:
            img = img.resize((ANCHO_MAX_CAPTURA, round(img.height * ANCHO_MAX_CAPTURA / img.width)))
        archivo = f"{hms(t).replace(':', '-')}.webp"
        img.save(self.carpeta / "capturas" / archivo, "WEBP", quality=80)
        self.capturas.append({"t": t, "archivo": archivo})
        self.guardar()
        return archivo

    def terminar(self, duracion: float) -> None:
        self.fin = datetime.now().isoformat(timespec="seconds")
        self.duracion = round(duracion, 2)
        self.estado = "grabado"
        self.guardar()
