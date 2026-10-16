// Recibe el estado de las pestañas de reunión, avisa al servidor de Python y captura la
// pestaña de la llamada cada N segundos. El estado se guarda en storage.session porque el
// service worker puede reiniciarse en cualquier momento.

const SERVIDOR = "http://127.0.0.1:8765";
const INICIAL = { tabId: null, silenciado: false, hablando: "", ultimaCaptura: 0, ultimoLatido: 0, intervalo: 15, ignorarTab: null, error: null };

async function api(ruta, cuerpo) {
  const r = await fetch(SERVIDOR + ruta, {
    method: cuerpo ? "POST" : "GET",
    headers: { "X-Llamadas": "1", ...(cuerpo ? { "Content-Type": "application/json" } : {}) },
    body: cuerpo ? JSON.stringify(cuerpo) : undefined,
  });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

async function leer() {
  return { ...INICIAL, ...(await chrome.storage.session.get(Object.keys(INICIAL))) };
}

const guardar = (cambios) => chrome.storage.session.set(cambios);

// Serializa el manejo de mensajes para que no se pisen (llegan uno por segundo por frame).
let cadena = Promise.resolve();
const enSerie = (fn) => (cadena = cadena.then(fn).catch((e) => console.warn("[llamadas]", e)));

function insignia(texto, color) {
  chrome.action.setBadgeText({ text: texto });
  if (color) chrome.action.setBadgeBackgroundColor({ color });
}

async function fallo(e) {
  await guardar({ error: String(e.message || e) });
  insignia("!", "#b45309");
}

async function iniciar(tabId, msg) {
  try {
    const estado = await api("/evento", { tipo: "inicio", plataforma: msg.plataforma, titulo: msg.titulo,
                                          url: msg.url, silenciado: msg.silenciado });
    await guardar({ tabId, silenciado: msg.silenciado, ultimaCaptura: 0,
                    intervalo: estado.intervalo_capturas || 15, error: null });
    insignia("REC", "#dc2626");
  } catch (e) {
    await fallo(e); // se reintenta solo con el siguiente mensaje de la pestaña
  }
}

async function terminar() {
  await guardar({ tabId: null });
  insignia("");
  try {
    await api("/evento", { tipo: "fin" });
  } catch (e) {
    await fallo(e);
  }
}

async function capturar(tabId) {
  const tab = await chrome.tabs.get(tabId);
  if (!tab.active) return; // la pestaña de la llamada no es la visible en su ventana
  const ventana = await chrome.windows.get(tab.windowId);
  if (ventana.state === "minimized") return;
  await guardar({ ultimaCaptura: Date.now() });
  const imagen = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "jpeg", quality: 80 });
  const r = await api("/captura", { imagen });
  if (!r.grabando) await guardar({ tabId: null }); // el servidor perdió la sesión: se reinicia sola
}

async function alRecibirEstado(msg, tabId) {
  const st = await leer();

  if (st.ignorarTab === tabId) {
    if (!msg.enLlamada) await guardar({ ignorarTab: null }); // salió de esa llamada
    return;
  }
  if (st.tabId === null) {
    if (msg.enLlamada) await iniciar(tabId, msg);
    return;
  }
  if (st.tabId !== tabId) return; // ya se graba otra pestaña

  if (!msg.enLlamada) return terminar();

  // Latido: si el servidor se reinició y perdió la sesión, se vuelve a iniciar (la retoma).
  if (Date.now() - st.ultimoLatido > 5000) {
    await guardar({ ultimoLatido: Date.now() });
    const sv = await api("/estado").catch(() => null);
    if (sv && !sv.grabando) return guardar({ tabId: null, hablando: "" });
  }

  if (msg.silenciado !== st.silenciado) {
    await guardar({ silenciado: msg.silenciado });
    await api("/evento", { tipo: "mic", silenciado: msg.silenciado }).catch(fallo);
  }
  const hablando = (msg.hablando || []).join("|");
  if (hablando !== st.hablando) {
    await guardar({ hablando });
    await api("/evento", { tipo: "hablantes", detalle: msg.hablando || [] }).catch(fallo);
  }
  if (Date.now() - st.ultimaCaptura >= st.intervalo * 1000) {
    await capturar(tabId).catch((e) => console.warn("[llamadas] captura:", e));
  }
}

chrome.runtime.onMessage.addListener((msg, sender, responder) => {
  if (msg.tipo === "estado" && sender.tab) {
    enSerie(() => alRecibirEstado(msg, sender.tab.id));
    return;
  }
  if (msg.tipo === "popup-estado") {
    enSerie(async () => {
      const st = await leer();
      let servidor = null;
      try { servidor = await api("/estado"); } catch (e) { st.error = String(e.message || e); }
      responder({ ...st, servidor });
    });
    return true;
  }
  if (msg.tipo === "popup-detener") {
    enSerie(async () => {
      const st = await leer();
      if (st.tabId !== null) {
        await guardar({ ignorarTab: st.tabId });
        await terminar();
      }
      responder({ ok: true });
    });
    return true;
  }
});

// Cerrar la pestaña o recargarla también termina la llamada.
chrome.tabs.onRemoved.addListener((tabId) =>
  enSerie(async () => {
    const st = await leer();
    if (st.tabId === tabId) await terminar();
    if (st.ignorarTab === tabId) await guardar({ ignorarTab: null });
  }));

chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (info.status !== "loading") return;
  enSerie(async () => {
    const st = await leer();
    if (st.tabId === tabId) await terminar();
  });
});

// Al instalar o recargar la extensión, activarla también en las reuniones que ya estaban
// abiertas, para no tener que recargar la pestaña (y salirse de la llamada).
chrome.runtime.onInstalled.addListener(async () => {
  const urls = chrome.runtime.getManifest().content_scripts[0].matches;
  for (const tab of await chrome.tabs.query({ url: urls })) {
    chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, files: ["content.js"] })
      .catch((e) => console.warn("[llamadas] no se pudo activar en", tab.url, e));
  }
});
