const $ = (id) => document.getElementById(id);
const NOMBRES = { meet: "Google Meet", teams: "Microsoft Teams", zoom: "Zoom" };

function fila(clave, valor) {
  const dt = document.createElement("dt");
  const dd = document.createElement("dd");
  dt.textContent = clave;
  dd.textContent = valor;
  $("datos").append(dt, dd);
}

async function actualizar() {
  const st = await chrome.runtime.sendMessage({ tipo: "popup-estado" });
  const sv = st.servidor;
  $("datos").replaceChildren();
  $("error").hidden = !st.error;
  $("error").textContent = !sv
    ? "No hay conexión con el servidor. Ejecuta: llamada servidor"
    : st.error;

  const grabando = !!sv?.grabando;
  $("punto").classList.toggle("rec", grabando);
  $("titulo").textContent = grabando ? "Grabando" : "Esperando una llamada";
  $("detener").hidden = !grabando;

  if (grabando) {
    fila("Reunión", sv.titulo);
    fila("Plataforma", NOMBRES[sv.plataforma] || sv.plataforma);
    fila("Duración", sv.tiempo);
    fila("Micrófono", sv.mic_silenciado ? "🔇 silenciado" : "🎙️ activo");
    fila("Capturas", String(sv.capturas));
  } else if (sv) {
    fila("Servidor", "conectado ✅");
  }
  if (sv?.en_cola) fila("Procesando", `${sv.en_cola} reunión(es) en cola`);
}

$("detener").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ tipo: "popup-detener" });
  actualizar();
});

actualizar();
setInterval(actualizar, 1000);
