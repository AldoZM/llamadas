# Llamadas

Graba tus reuniones de **Google Meet, Microsoft Teams y Zoom** (versión web, en Brave/Chrome/Edge), las transcribe en tu PC y genera notas en tu vault de Obsidian con capturas de pantalla y un resumen hecho por IA.

```
Brave (extensión)                         Python (servidor local 127.0.0.1:8765)
 • detecta que entraste a la llamada  ──▶  • graba: canal izq = tu micrófono, der = los demás
 • detecta si silencias tu micrófono  ──▶  • silencia tu canal cuando te silencias en la llamada
 • captura la pestaña cada 15 s       ──▶  • guarda capturas (descarta las repetidas)
                                           • al colgar: transcribe → notas de Obsidian → resumen IA
```

## Instalación

### 1. Python
```bash
cd ~/llamadas
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```
En Windows: `py -m venv .venv` y `.venv\Scripts\pip install -r requirements.txt`.

### 2. Extensión de Brave
1. Abre `brave://extensions`
2. Activa **Modo de desarrollador** (arriba a la derecha)
3. **Cargar extensión sin empaquetar** → elige la carpeta `~/llamadas/extension`
4. Fija el icono de *Llamadas* en la barra para ver el estado.

> Pide acceso a "todos los sitios" porque Chromium solo permite capturar la pestaña visible con ese permiso. Solo actúa en Meet, Teams y Zoom.

### 3. Resumen con IA
Por defecto lo hace **AGY** (Antigravity CLI) con tu sesión de esta PC, sin API key. Alternativas en `config.toml` → `motor`: `"claude"` (Claude Code CLI) o `"api"` (API de Claude con `ANTHROPIC_API_KEY`). El agente corre en modo de solo lectura: lee la reunión, no modifica nada.

## Uso

```bash
./llamada servidor        # déjalo corriendo (o instálalo como servicio, ver abajo)
```
Entra a tu reunión en el navegador: la grabación empieza sola (icono **REC**) y termina al colgar o cerrar la pestaña. Después se procesa en segundo plano.

| Comando | Para qué |
|---|---|
| `llamada probar-audio` | Graba 5 s y dice si llega señal del mic y de la salida |
| `llamada dispositivos` | Muestra qué micrófono y salida se van a grabar |
| `llamada procesar [reunión]` | Regenera notas y resumen reutilizando la transcripción (por defecto, la última) |
| `llamada procesar --retranscribir` | Igual, pero vuelve a transcribir el audio (lento) |
| `llamada analizar [reunión]` | Solo regenera el resumen con IA |

## Resultado en Obsidian

```
Llamadas/Reuniones/2026-10-16 1530 Revisión de presupuesto/
├── 2026-10-16 1530 Revisión de presupuesto - Resumen.md
├── 2026-10-16 1530 Revisión de presupuesto - Transcripción.md
├── audio.ogg                 (estéreo: izquierda = tú, derecha = los demás)
├── capturas/00-03-45.webp
└── .datos/                   (oculto en Obsidian: sesión, transcripción en JSON)
```
- Cada frase lleva el nombre de quien hablaba según la llamada (en Meet, por el indicador de voz de cada recuadro). Tu micrófono aparece como "Yo"; si no se pudo saber quién hablaba, "Otros".
- La transcripción intercala capturas y marcas 🔇/🎙️ en el momento en que ocurrieron.
- En el resumen, cada marca de tiempo es un enlace al párrafo exacto de la transcripción.

## Ajustes
Todo está en `config.toml` (vault, modelo de Whisper, intervalo de capturas, umbral de duplicados, modelo de Claude…). El prompt del resumen está en `prompt_analisis.md`: edítalo para cambiar las secciones.

## Consejos
- **Usa audífonos.** Con bocinas tu micrófono capta a los demás; hay un filtro anti-eco, pero con audífonos el resultado es más limpio.
- **Las capturas son de la pestaña visible.** Si cambias a otra pestaña de la misma ventana, no se captura en ese momento (la grabación de audio sigue). Lo ideal es tener la reunión en su propia ventana.
- **Avísale a los participantes** que estás grabando.
- Si una plataforma cambia su interfaz y deja de detectarse la llamada o el micrófono, los selectores están en `extension/content.js`.

## Arranque automático (recomendado)

**Linux (systemd, activación por socket):** el servidor no corre mientras no estés en una llamada. systemd escucha el puerto 8765 y lo arranca en cuanto la extensión detecta que entraste a una reunión. Al terminar procesa la reunión y, tras 15 minutos sin actividad, se apaga solo.
```bash
mkdir -p ~/.config/systemd/user
cp contrib/llamadas.socket contrib/llamadas.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now llamadas.socket
journalctl --user -u llamadas -f     # ver el log
```
Para desactivarlo: `systemctl --user disable --now llamadas.socket llamadas.service`.

**Windows:** crea un acceso directo a `llamada.bat servidor` en `shell:startup` (queda corriendo en segundo plano; consume muy poco mientras no hay llamada).
