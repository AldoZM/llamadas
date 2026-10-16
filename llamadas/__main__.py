import argparse
import logging
import sys
import time
from pathlib import Path

from . import config


def _carpeta(cfg: config.Config, texto: str | None) -> Path:
    """Acepta una ruta, o el nombre (o parte) de una reunión; sin argumento, la más reciente."""
    if texto and Path(texto).expanduser().is_dir():
        return Path(texto).expanduser().resolve()
    candidatas = sorted(p.parent.parent for p in cfg.reuniones.glob("*/.datos/sesion.json"))
    if texto:
        candidatas = [c for c in candidatas if texto.lower() in c.name.lower()]
    if not candidatas:
        sys.exit(f"No encontré ninguna reunión{f' que coincida con «{texto}»' if texto else ''}.")
    return candidatas[-1]


def main() -> None:
    p = argparse.ArgumentParser(prog="llamada", description="Graba, transcribe y resume llamadas.")
    sub = p.add_subparsers(dest="comando")
    sub.add_parser("servidor", help="Inicia el servidor que usa la extensión (por defecto)")
    for nombre, ayuda in [("procesar", "Repite transcripción + notas + resumen de una reunión"),
                          ("analizar", "Solo vuelve a generar el resumen con IA")]:
        sp = sub.add_parser(nombre, help=ayuda)
        sp.add_argument("reunion", nargs="?", help="Ruta o parte del nombre (por defecto, la última)")
        if nombre == "procesar":
            sp.add_argument("--retranscribir", action="store_true",
                            help="Vuelve a transcribir el audio (lento); sin esto reutiliza la transcripción")
    sp = sub.add_parser("probar-audio", help="Graba unos segundos para comprobar mic y salida")
    sp.add_argument("segundos", nargs="?", type=int, default=5)
    sub.add_parser("dispositivos", help="Lista los dispositivos de audio")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    for ruidoso in ("faster_whisper", "httpx", "httpcore", "huggingface_hub", "anthropic"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)
    cfg = config.cargar()

    if args.comando in (None, "servidor"):
        from .servidor import ejecutar
        ejecutar(cfg)

    elif args.comando in ("procesar", "analizar"):
        from .procesar import procesar
        carpeta = _carpeta(cfg, args.reunion)
        if args.comando == "procesar" and args.retranscribir:
            (carpeta / ".datos" / "transcripcion.json").unlink(missing_ok=True)
        procesar(carpeta, cfg, solo_analizar=args.comando == "analizar")

    elif args.comando == "dispositivos":
        from .grabadora import dispositivo_mic, dispositivo_salida
        print("Micrófono:      ", dispositivo_mic().name)
        print("Salida (loopback):", dispositivo_salida().name)

    elif args.comando == "probar-audio":
        import numpy as np
        import soundfile as sf
        from .grabadora import NOMBRES, Grabadora
        destino = Path.home() / ".cache" / "llamadas-prueba"
        g = Grabadora(destino, cfg.frecuencia, True, lambda: False)
        print(f"Grabando {args.segundos} s: habla y reproduce algo de audio...")
        g.iniciar()
        time.sleep(args.segundos)
        g.detener()
        for n in NOMBRES.values():
            audio, _ = sf.read(destino / n)
            nivel = float(np.sqrt(np.mean(audio ** 2))) if len(audio) else 0.0
            print(f"  {n:12s} nivel {nivel:.4f} {'✅' if nivel > 1e-3 else '⚠️  sin señal'}")
        print(f"Archivos en {destino}")
        for e in g.errores:
            print("  ❌", e)


if __name__ == "__main__":
    main()
