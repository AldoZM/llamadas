// Detecta si estás dentro de una llamada y si tu micrófono está silenciado, leyendo los
// botones de la propia página. Cada segundo le envía el estado al service worker.
//
// Los selectores dependen del HTML de cada plataforma, que cambia con el tiempo: si algo deja
// de detectarse, este es el único archivo que hay que ajustar.

(() => {
const PLATAFORMAS = [
  {
    id: "meet",
    host: /(^|\.)meet\.google\.com$/,
    colgar: [
      'button[jsname="CQylAd"]',
      'button[aria-label*="Leave call" i]',
      'button[aria-label*="Salir de la llamada" i]',
      'button[aria-label*="Abandonar la llamada" i]',
    ],
    mic: [
      'button[data-is-muted][aria-label*="micr" i]',
      '[role="button"][data-is-muted][aria-label*="micr" i]',
    ],
    recuadro: "[data-participant-id]",
    // Clases que anima el indicador de voz de Meet (vistas el 2026-10-16).
    indicadores: ["Oaajhc", "wEsLMd", "sxlEM", "BlxGDf", "HX2H7", "OgVli", "gjg47c"],
    titulo: () =>
      document.querySelector("[data-meeting-title]")?.getAttribute("data-meeting-title") ||
      document.title.replace(/^Meet\s*[-–:]\s*/i, "") || "Google Meet",
  },
  {
    id: "teams",
    host: /(^|\.)teams\.(microsoft|live)\.com$|^teams\.cloud\.microsoft$/,
    colgar: [
      "#hangup-button",
      'button[data-tid="hangup-main-btn"]',
      'button[data-tid="call-hangup"]',
      'button[data-tid="hangup-button"]',
    ],
    mic: [
      "#microphone-button",
      'button[data-tid="microphone-button"]',
      'button[data-tid="toggle-mute"]',
    ],
    titulo: () => document.title.replace(/\s*\|\s*Microsoft Teams.*$/i, "").replace(/^\(\d+\)\s*/, "") || "Microsoft Teams",
  },
  {
    id: "zoom",
    host: /(^|\.)zoom\.(us|com)$/,
    colgar: [
      ".footer__leave-btn",
      "button.footer__leave-btn-container",
      'button[aria-label="Leave"]',
      'button[aria-label="Salir"]',
      'button[aria-label="End"]',
      'button[aria-label="Finalizar"]',
    ],
    mic: [
      "button.join-audio-container__btn",
      'button[aria-label*="microphone" i]',
      'button[aria-label*="micrófono" i]',
      'button[aria-label*="join audio" i]',
      'button[aria-label*="audio" i][class*="audio"]',
    ],
    titulo: () =>
      document.querySelector(".meeting-info-container__topic, .meeting-title, .meeting-info-icon__topic")
        ?.textContent?.trim() || "Reunión de Zoom",
  },
];

const plataforma = PLATAFORMAS.find((p) => p.host.test(location.hostname));

function buscar(selectores) {
  for (const s of selectores) {
    const el = document.querySelector(s);
    if (el) return el;
  }
  return null;
}

// true = silenciado, false = activo, null = no se sabe.
function estaSilenciado(boton) {
  if (!boton) return null;
  const muted = boton.getAttribute("data-is-muted"); // Meet
  if (muted !== null) return muted === "true";
  const estado = boton.getAttribute("data-state"); // Teams: "mic" / "mic-off"
  if (estado && /mic/i.test(estado)) return /off/i.test(estado);
  const texto = (boton.getAttribute("aria-label") || boton.title || boton.textContent || "").toLowerCase();
  // La etiqueta describe la acción del botón: "Activar micrófono" significa que ahora está silenciado.
  if (/join audio|unirse al audio|conectar audio|entrar al audio/.test(texto)) return true;
  if (/\bunmute\b|\b(re)?activar\b|\bturn on\b|\bencender\b/.test(texto)) return true;
  if (/\bmute\b|\bsilenciar\b|\bdesactivar\b|\bturn off\b|\bapagar\b/.test(texto)) return false;
  return null;
}

let vioControles = false; // este frame tiene la interfaz de la llamada (Zoom la mete en un iframe)
let enLlamada = false;
let seguidos = 0; // lecturas seguidas que contradicen el estado actual (antirrebote)
let silenciado = false;

// Quién habla: el indicador de voz de cada recuadro se anima (cambia de clase) mientras esa
// persona habla. Si la plataforma tiene clases de indicador conocidas, solo cuentan esas (así se
// ignora el paso del cursor o el reacomodo de recuadros); si ya no aparecen (la plataforma cambió
// su HTML), se cuenta cualquier cambio de clase dentro del recuadro.
const actividad = new Map(); // recuadro -> puntos en el último segundo
const indicadores = new Set(plataforma?.indicadores || []);
let ultimoIndicador = 0;
if (plataforma?.recuadro) {
  new MutationObserver((mutaciones) => {
    const ahora = Date.now();
    for (const m of mutaciones) {
      const recuadro = m.target.closest?.(plataforma.recuadro);
      if (!recuadro) continue;
      const antes = new Set((m.oldValue || "").split(/\s+/));
      const despues = new Set(m.target.classList);
      const cambiadas = [...antes, ...despues].filter((k) => k && antes.has(k) !== despues.has(k));
      let puntos = 0;
      if (cambiadas.some((k) => indicadores.has(k))) {
        puntos = 3;
        ultimoIndicador = ahora;
      } else if (ahora - ultimoIndicador > 120000) {
        puntos = 1; // modo general: no se han visto los indicadores conocidos en 2 min
      }
      if (puntos) actividad.set(recuadro, (actividad.get(recuadro) || 0) + puntos);
    }
  }).observe(document.documentElement,
             { subtree: true, attributes: true, attributeFilter: ["class"], attributeOldValue: true });
}

const nombres = new Map(); // participante -> nombre (se lee una vez y se reutiliza)
function nombreDe(recuadro) {
  const id = recuadro.getAttribute("data-participant-id") || recuadro;
  if (nombres.has(id)) return nombres.get(id);
  const textos = [];
  const recorrido = document.createTreeWalker(recuadro, NodeFilter.SHOW_TEXT);
  while (recorrido.nextNode()) {
    const nodo = recorrido.currentNode;
    if (nodo.parentElement?.closest('button, [role="button"], [role="tooltip"], [aria-hidden="true"]')) continue;
    const t = nodo.textContent.trim();
    if (t.length > 2 && !/^[a-z_]+$/.test(t)) textos.push(t); // descarta iconos ("mic_off")
  }
  let nombre = textos[0] || null;
  if (nombre) {
    nombre = nombre.replace(/^(No fijar|Fijar a|Presentación de|Presentation by)\s+(la presentación de\s+)?/i, "")
                   .replace(/\s*\((tú|you|presentación|presentando|presenting)\)$/i, "")
                   .replace(/\s+(a|to) tu pantalla principal$/i, "").trim();
    nombres.set(id, nombre);
  }
  return nombre;
}

function quienesHablan() {
  const hablando = [...actividad.entries()]
    .filter(([recuadro, n]) => n >= 3 && recuadro.isConnected)
    .sort((a, b) => b[1] - a[1])
    .map(([recuadro]) => nombreDe(recuadro))
    .filter(Boolean);
  actividad.clear();
  return [...new Set(hablando)].slice(0, 3);
}

function revisar() {
  const colgar = buscar(plataforma.colgar);
  const mic = buscar(plataforma.mic);
  if (colgar || mic) vioControles = true;
  if (!vioControles) return;

  const ahora = !!colgar;
  if (ahora !== enLlamada) {
    seguidos += 1;
    // 2 s para entrar, 4 s para salir: la interfaz a veces parpadea al re-renderizar.
    if (seguidos >= (ahora ? 2 : 4)) {
      enLlamada = ahora;
      seguidos = 0;
    }
  } else {
    seguidos = 0;
  }
  const s = estaSilenciado(mic);
  if (s !== null) silenciado = s;

  try {
    chrome.runtime.sendMessage({
      tipo: "estado",
      plataforma: plataforma.id,
      titulo: plataforma.titulo(),
      url: location.href,
      enLlamada,
      silenciado,
      hablando: quienesHablan(),
    }).catch(() => {});
  } catch {
    clearInterval(temporizador); // la extensión se recargó: este script quedó huérfano
  }
}

const temporizador = plataforma ? setInterval(revisar, 1000) : null;
})();
