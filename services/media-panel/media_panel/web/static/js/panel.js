const app = document.getElementById("app");
const title = document.getElementById("title");
const tabs = [...document.querySelectorAll(".tabs button")];

let rulesState = null;
let currentRuleSection = "entrada";

const RULE_SECTIONS = {
  entrada: {
    title: "Entrada",
    help: "Formatos de video que acepta el motor de media.",
    groups: [
      {
        title: "Formatos",
        note: "Extensiones usadas por el detector de media.",
        controls: [
          { type: "list", path: "entrada.extensiones_video", label: "Extensiones de video" }
        ]
      }
    ]
  },
  video: {
    title: "Video",
    help: "Pista de video que acepta y como queda marcada.",
    groups: [
      {
        title: "Validacion",
        note: "El motor espera una pista de video clara.",
        controls: [
          { type: "number", path: "video.pistas_exactas", label: "Pistas permitidas", min: 1, max: 3, step: 1 },
          { type: "list", path: "video.idiomas_aceptados", label: "Idiomas aceptados" },
          { type: "list", path: "video.idiomas_indeterminados_como_es", label: "Indeterminados como ES" }
        ]
      },
      {
        title: "Correccion por audio",
        note: "Permite corregir el idioma de video si el audio espanol es valido.",
        controls: [
          { type: "boolean", path: "video.aceptar_por_audio_es", label: "Aceptar por audio ES" },
          { type: "list", path: "video.idiomas_corregibles_por_audio_es", label: "Idiomas corregibles" },
          { type: "text", path: "video.idioma_final_por_audio_es", label: "Idioma final por audio" }
        ]
      },
      {
        title: "Salida",
        note: "Etiquetas finales de la pista de video.",
        controls: [
          { type: "text", path: "video.idioma_final", label: "Idioma final" },
          { type: "boolean", path: "video.marcar_default", label: "Marcar default" },
          { type: "boolean", path: "video.marcar_forzado", label: "Marcar forzado" }
        ]
      }
    ]
  },
  audio: {
    title: "Audio",
    help: "Eleccion de audio y conversion si hace falta.",
    groups: [
      {
        title: "Idiomas",
        note: "Audios que pueden quedarse.",
        controls: [
          { type: "list", path: "audio.idiomas_aceptados", label: "Idiomas aceptados" },
          { type: "boolean", path: "audio.aceptar_indeterminado_si_video_es", label: "Aceptar indeterminado con video ES" },
          { type: "list", path: "audio.idiomas_condicionales_si_video_es", label: "Idiomas condicionales" },
          { type: "text", path: "audio.idioma_final_condicional", label: "Idioma final condicional" }
        ]
      },
      {
        title: "Conversion",
        note: "Audio multicanal que se convierte a AC3.",
        controls: [
          { type: "number", path: "audio.canales_convertir_ac3_desde", label: "Convertir desde canales", min: 2, max: 12, step: 1 },
          { type: "text", path: "audio.bitrate_ac3", label: "Bitrate AC3" },
          { type: "text", path: "audio.titulo_ac3_convertido", label: "Titulo AC3" }
        ]
      },
      {
        title: "Prioridad y salida",
        note: "Ranking de codecs y nombres visibles.",
        controls: [
          { type: "kv-number", path: "audio.codec_prioridad", label: "Prioridad codec" },
          { type: "kv-text", path: "audio.titulos_codec", label: "Titulos codec" },
          { type: "boolean", path: "audio.marcar_default", label: "Marcar default" },
          { type: "boolean", path: "audio.marcar_forzado", label: "Marcar forzado" }
        ]
      }
    ]
  },
  subtitulos: {
    title: "Subtitulos",
    help: "Forzados, subtitulos de imagen y salida SRT.",
    groups: [
      {
        title: "Aceptados",
        note: "Idiomas y formatos de texto que puede procesar.",
        controls: [
          { type: "list", path: "subtitulos.idiomas_aceptados", label: "Idiomas aceptados" },
          { type: "list", path: "subtitulos.formatos_texto_aceptados", label: "Formatos texto" },
          { type: "list", path: "subtitulos.formatos_imagen_no_aceptados", label: "Formatos imagen no aceptados" }
        ]
      },
      {
        title: "Reglas de frases",
        note: "Decide si un subtitulo parece forzado real.",
        controls: [
          { type: "number", path: "subtitulos.frases_descartar_hasta", label: "Descartar hasta frases", min: 0, max: 50, step: 1 },
          { type: "number", path: "subtitulos.frases_maximo_unico_forzado", label: "Maximo unico forzado", min: 1, max: 2000, step: 1 },
          { type: "select", path: "subtitulos.unico_es_modo", label: "Unico ES", options: [
            { value: "aplicar_limite", label: "Aplicar limite" },
            { value: "aceptar_siempre", label: "Aceptar siempre" }
          ] },
          { type: "select", path: "subtitulos.sin_subtitulos_modo", label: "Sin subtitulos", options: [
            { value: "procesar_sin_subtitulos", label: "Procesar sin subtitulos" },
            { value: "cuarentena", label: "Mandar a revision" }
          ] }
        ]
      },
      {
        title: "Delay Audio",
        note: "Regla para aceptar subtitulos generados por Delay Audio.",
        controls: [
          { type: "boolean", path: "subtitulos.delay_audio.activo", label: "Activo" },
          { type: "text", path: "subtitulos.delay_audio.texto_titulo", label: "Texto en titulo" },
          { type: "number", path: "subtitulos.delay_audio.frases_maximo", label: "Maximo frases", min: 1, max: 1000, step: 1 }
        ]
      },
      {
        title: "Salida",
        note: "Como queda el subtitulo final.",
        controls: [
          { type: "text", path: "subtitulos.titulo_final", label: "Titulo interno" },
          { type: "text", path: "subtitulos.sufijo_srt_externo", label: "Sufijo SRT externo" },
          { type: "boolean", path: "subtitulos.interno_default", label: "Interno default" },
          { type: "boolean", path: "subtitulos.interno_forzado", label: "Interno forzado" }
        ]
      }
    ]
  },
  limpieza: {
    title: "Limpieza",
    help: "Metadatos, capitulos y salida final.",
    groups: [
      {
        title: "Capitulos",
        note: "Capitulos generados si el archivo no trae capitulos utiles.",
        controls: [
          { type: "boolean", path: "limpieza.crear_capitulos", label: "Crear capitulos" },
          { type: "number", path: "limpieza.capitulo_cada_segundos", label: "Cada", suffix: "segundos", min: 60, max: 3600, step: 60 }
        ]
      },
      {
        title: "Salida limpia",
        note: "Limpieza del MKV final.",
        controls: [
          { type: "boolean", path: "limpieza.borrar_metadata_original", label: "Borrar metadata original" },
          { type: "boolean", path: "limpieza.limpiar_tags_mkv", label: "Limpiar tags MKV" },
          { type: "boolean", path: "limpieza.exportar_srt_externo", label: "Exportar SRT externo" }
        ]
      }
    ]
  },
  trailers: {
    title: "Trailers",
    help: "Emparejamiento y nombre final del trailer.",
    groups: [
      {
        title: "Trailers",
        note: "Emparejamiento y nombre final del trailer.",
        controls: [
          { type: "list", path: "trailers.extensiones_video", label: "Extensiones" },
          { type: "number", path: "trailers.score_minimo_con_ano", label: "Score con ano", min: 0, max: 1, step: 0.01 },
          { type: "number", path: "trailers.score_minimo_sin_ano", label: "Score sin ano", min: 0, max: 1, step: 0.01 },
          { type: "text", path: "trailers.nombre_final", label: "Nombre final" },
          { type: "select", path: "trailers.si_existe", label: "Si existe", options: [
            { value: "renombrar_sin_borrar", label: "Renombrar sin borrar" },
            { value: "sustituir_anterior", label: "Sustituir anterior" }
          ] },
          { type: "list", path: "trailers.palabras_ruido_titulo", label: "Ruido del titulo" }
        ]
      }
    ]
  }
};

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
  }[ch]));
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    ...options
  });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

function getPath(obj, path) {
  return path.split(".").reduce((acc, key) => acc && acc[key] !== undefined ? acc[key] : undefined, obj);
}

function setPath(obj, path, value) {
  const parts = path.split(".");
  let cursor = obj;
  for (let i = 0; i < parts.length - 1; i++) {
    const key = parts[i];
    if (!cursor[key] || typeof cursor[key] !== "object" || Array.isArray(cursor[key])) cursor[key] = {};
    cursor = cursor[key];
  }
  cursor[parts[parts.length - 1]] = value;
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function pill(text, type = "info") {
  return `<span class="pill ${type}">${esc(text)}</span>`;
}

function formatTime(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleString("es-ES");
}

function setActive(view) {
  tabs.forEach(btn => btn.classList.toggle("active", btn.dataset.view === view));
}

async function showMotor() {
  setActive("motor");
  title.textContent = "Estado del motor";
  app.innerHTML = `<section class="panel">Cargando motor...</section>`;
  const [status, jobs] = await Promise.all([api("/api/status"), api("/api/jobs")]);
  const orchOk = status.orchestrator?.status === "ok";
  const workerOk = status.media_worker?.status === "ok";
  const deps = status.orchestrator?.dependencies || {};
  const latest = (jobs.jobs || []).slice(0, 8);
  app.innerHTML = `
    <section class="grid">
      <div class="card"><small>Orquestador</small><span class="metric">${orchOk ? "OK" : "Error"}</span>${pill(orchOk ? "activo" : "fallo", orchOk ? "ok" : "bad")}</div>
      <div class="card"><small>Media Worker</small><span class="metric">${workerOk ? "OK" : "Error"}</span>${pill(workerOk ? "activo" : "fallo", workerOk ? "ok" : "bad")}</div>
      <div class="card"><small>qBittorrent</small><span class="metric">${esc(deps.qbittorrent || "-")}</span></div>
      <div class="card"><small>RDT-Client</small><span class="metric">${esc(deps.rdtclient || "-")}</span></div>
    </section>
    <section class="panel" style="margin-top:14px">
      <h2>Rutas vivas</h2>
      <div class="grid">
        ${Object.entries(status.paths || {}).map(([key, value]) => `
          <div class="card"><small>${esc(key)}</small><span class="metric">${Number(value.items || 0)}</span><div class="muted">${esc(value.path)}</div></div>
        `).join("")}
      </div>
    </section>
    <section class="panel" style="margin-top:14px">
      <h2>Ultimos trabajos</h2>
      ${jobsTable(latest)}
    </section>`;
}

function jobsTable(jobs, options = {}) {
  if (!jobs.length) return `<div class="empty">No hay trabajos registrados.</div>`;
  const actions = options.actions !== false;
  return `<table class="table">
    <thead><tr><th>Nombre</th><th>Categoria</th><th>Estado</th><th>Actualizado</th>${actions ? "<th>Diagnostico</th>" : ""}</tr></thead>
    <tbody>${jobs.map(job => `<tr>
      <td>${esc(job.name)}</td>
      <td>${esc(job.category)}</td>
      <td>${pill(stateLabel(job.state), stateTone(job.state))}</td>
      <td>${esc(formatTime(job.updated_at))}</td>
      ${actions ? `<td><button class="btn ghost small" data-codex-job="${esc(job.job_id)}">Informe Codex</button></td>` : ""}
    </tr>`).join("")}</tbody>
  </table>`;
}

function stateLabel(state) {
  return {
    ready_stage: "listo para taller",
    staging: "en taller"
  }[state] || state;
}

function stateTone(state) {
  if (state === "done") return "ok";
  if (["duplicate", "manual_review"].includes(state)) return "warn";
  if (String(state || "").includes("error")) return "bad";
  return "info";
}

async function showHistorial() {
  setActive("historial");
  title.textContent = "Historial";
  app.innerHTML = `<section class="panel">Cargando historial...</section>`;
  const data = await api("/api/jobs");
  const jobs = data.jobs || [];
  app.innerHTML = `<section class="panel"><h2>Trabajos recientes</h2>${jobsTable(jobs)}</section>`;
}

async function showRevision() {
  setActive("revision");
  title.textContent = "Revision";
  app.innerHTML = `<section class="panel">Cargando revision...</section>`;
  const data = await api("/api/review");
  const items = data.items || [];
  app.innerHTML = `<section class="panel">
    <h2>repetidas_vs_error</h2>
    <div class="muted">${esc(data.review_dir)}</div>
    <div class="review-list" style="margin-top:12px">
      ${items.length ? items.map(item => `
        <article class="review-item">
          <div class="review-top">
            <div><b>${esc(item.name)}</b><div class="muted">${esc(item.path)}</div></div>
            ${pill(item.reason_file || item.phase || "revision", item.reason_file && item.reason_file.toLowerCase().includes("repetida") ? "warn" : "bad")}
          </div>
          ${item.reason_text ? `<pre class="pre">${esc(item.reason_text)}</pre>` : ""}
        </article>
      `).join("") : `<div class="empty">No hay elementos en revision.</div>`}
    </div>
  </section>`;
}

async function showInformes() {
  setActive("informes");
  title.textContent = "Informes";
  app.innerHTML = `<section class="panel">Cargando informes...</section>`;
  const [data, codex] = await Promise.all([api("/api/reports"), api("/api/codex-diagnostics")]);
  const files = data.files || [];
  const codexFiles = codex.files || [];
  const codexOrder = ["movies", "tv", "trailers", "repetidas_vs_error"];
  const groupedCodex = codexFiles.reduce((groups, file) => {
    const key = file.folder || "";
    if (!codexOrder.includes(key)) return groups;
    groups[key] = groups[key] || [];
    groups[key].push(file);
    return groups;
  }, {});
  const codexBlocks = codexOrder
    .filter(key => groupedCodex[key]?.length)
    .map(key => {
      const label = groupedCodex[key][0].folder_label || key;
      return `
        <details class="fold-group codex-group">
          <summary class="fold-head">
            <span class="fold-title">${esc(label)}</span>
            <span class="muted">${groupedCodex[key].length} informes</span>
          </summary>
          <div class="report-list compact">
            ${groupedCodex[key].map(file => `
              <div class="report-row codex-report-row">
                <div class="report-main">
                  <b>${esc(file.display_name || file.name)}</b>
                  <div class="muted">${esc(formatTime(file.updated_at || file.mtime))} · ${esc(stateLabel(file.state || "-"))} · ${esc(file.category || "-")}</div>
                  <small class="muted">${esc(file.name)}</small>
                </div>
                <div class="report-actions">
                  <span class="muted">${Math.round(Number(file.size || 0) / 1024)} KB</span>
                  <button class="btn ghost" data-codex-download="${esc(file.download_url)}">Descargar</button>
                </div>
              </div>
            `).join("")}
          </div>
        </details>`;
    })
    .join("");
  app.innerHTML = `
  <section class="panel">
    <h2>Informes Codex</h2>
    <div class="muted">${esc(codex.root || "")}</div>
    ${codexFiles.length ? codexBlocks : `<div class="empty" style="margin-top:12px">Aun no hay informes Codex.</div>`}
  </section>
  <section class="panel" style="margin-top:14px">
    <details class="fold-group worker-group">
      <summary class="fold-head">
        <span class="fold-title">Informes del worker</span>
        <span class="muted">${files.length} informes</span>
      </summary>
      <div class="muted fold-path">${esc(data.report_root)}</div>
      <div class="report-list" style="margin-top:12px">
        ${files.length ? files.map(file => `
          <div class="report-row">
            <b>${esc(file.relative)}</b>
            <span class="muted">${Math.round(Number(file.size || 0) / 1024)} KB</span>
            <button class="btn ghost" data-report="${esc(file.relative)}">Abrir</button>
          </div>
        `).join("") : `<div class="empty">Aun no hay informes.</div>`}
      </div>
      <div id="report-view" class="report-view" style="display:none;margin-top:12px"><pre></pre></div>
    </details>
  </section>`;
  document.querySelectorAll("[data-codex-download]").forEach(btn => btn.addEventListener("click", () => {
    location.href = btn.dataset.codexDownload;
  }));
  document.querySelectorAll("[data-report]").forEach(btn => btn.addEventListener("click", async () => {
    const file = btn.dataset.report;
    const text = await fetch(`/api/report?file=${encodeURIComponent(file)}`, { cache: "no-store" }).then(r => r.text());
    const box = document.getElementById("report-view");
    box.style.display = "block";
    box.querySelector("pre").textContent = text;
  }));
}

async function showReglas() {
  setActive("reglas");
  title.textContent = "Motor de reglas";
  app.innerHTML = `<section class="panel">Cargando reglas...</section>`;
  rulesState = await api("/api/rules");
  renderRules();
}

function renderRules() {
  const section = RULE_SECTIONS[currentRuleSection];
  const sectionButtons = Object.entries(RULE_SECTIONS).map(([key, value]) =>
    `<button class="${key === currentRuleSection ? "active" : ""}" data-rule-section="${key}">${esc(value.title)}</button>`
  ).join("");
  app.innerHTML = `
    <section class="split">
      <aside class="side">${sectionButtons}</aside>
      <div class="rules-work panel">
        <div class="toolbar">
          <div>
            <h2>${esc(section.title)}</h2>
            <div class="muted">${esc(section.help)}</div>
          </div>
          <div class="toolbar-actions">
            <button class="btn ghost" id="reload-rules">Recargar</button>
            <button class="btn primary" id="save-rules">Guardar reglas</button>
          </div>
        </div>
        <div id="rules-status" class="status">Archivo: ${esc(rulesState.rules_path)}</div>
        <div id="rules-editor">${section.groups.map(renderGroup).join("")}</div>
      </div>
    </section>`;

  document.querySelectorAll("[data-rule-section]").forEach(btn => btn.addEventListener("click", () => {
    currentRuleSection = btn.dataset.ruleSection;
    renderRules();
  }));
  document.getElementById("reload-rules").addEventListener("click", showReglas);
  document.getElementById("save-rules").addEventListener("click", saveRules);
}

function renderGroup(group) {
  return `<div class="rule-group">
    <h3>${esc(group.title)}</h3>
    <p>${esc(group.note || "")}</p>
    ${group.controls.map(renderControl).join("")}
  </div>`;
}

function renderControl(control) {
  const value = getPath(rulesState.rules, control.path);
  const id = `field-${control.path.replace(/[^a-z0-9]+/gi, "-")}`;
  const hint = control.suffix ? `<span class="hint">${esc(control.suffix)}</span>` : "";
  let input = "";
  if (control.type === "boolean") {
    input = `<label class="toggle"><input id="${id}" data-path="${esc(control.path)}" data-type="boolean" type="checkbox" ${value ? "checked" : ""}> Activo</label>`;
  } else if (control.type === "number") {
    input = `<input id="${id}" data-path="${esc(control.path)}" data-type="number" type="number" value="${esc(value ?? "")}" min="${esc(control.min ?? "")}" max="${esc(control.max ?? "")}" step="${esc(control.step ?? 1)}">${hint}`;
  } else if (control.type === "list") {
    input = `<textarea id="${id}" data-path="${esc(control.path)}" data-type="list">${esc((value || []).join("\n"))}</textarea><span class="hint">Una entrada por linea.</span>`;
  } else if (control.type === "kv-number" || control.type === "kv-text") {
    input = `<textarea id="${id}" data-path="${esc(control.path)}" data-type="${control.type}">${esc(Object.entries(value || {}).map(([k, v]) => `${k}: ${v}`).join("\n"))}</textarea><span class="hint">Formato: clave: valor</span>`;
  } else if (control.type === "select") {
    input = `<select id="${id}" data-path="${esc(control.path)}" data-type="text">
      ${(control.options || []).map(opt => `<option value="${esc(opt.value)}" ${opt.value === value ? "selected" : ""}>${esc(opt.label)}</option>`).join("")}
    </select>`;
  } else {
    input = `<input id="${id}" data-path="${esc(control.path)}" data-type="text" type="text" value="${esc(value ?? "")}">`;
  }
  return `<div class="field"><label for="${id}">${esc(control.label)}</label><div>${input}</div></div>`;
}

function collectRules() {
  const rules = clone(rulesState.rules);
  document.querySelectorAll("[data-path]").forEach(input => {
    const path = input.dataset.path;
    const type = input.dataset.type;
    let value;
    if (type === "boolean") {
      value = input.checked;
    } else if (type === "number") {
      value = Number(input.value);
    } else if (type === "list") {
      value = input.value.split(/\r?\n/).map(x => x.trim()).filter(Boolean);
    } else if (type === "kv-number" || type === "kv-text") {
      value = {};
      input.value.split(/\r?\n/).forEach(line => {
        const idx = line.indexOf(":");
        if (idx <= 0) return;
        const key = line.slice(0, idx).trim();
        const raw = line.slice(idx + 1).trim();
        if (!key) return;
        value[key] = type === "kv-number" ? Number(raw) : raw;
      });
    } else {
      value = input.value;
    }
    setPath(rules, path, value);
  });
  return rules;
}

async function saveRules() {
  const btn = document.getElementById("save-rules");
  const status = document.getElementById("rules-status");
  btn.disabled = true;
  status.textContent = "Guardando...";
  try {
    const rules = collectRules();
    rulesState = await api("/api/rules", {
      method: "POST",
      body: JSON.stringify({ rules })
    });
    status.textContent = "Reglas guardadas correctamente.";
    renderRules();
  } catch (error) {
    status.textContent = `Error guardando: ${error.message}`;
  } finally {
    btn.disabled = false;
  }
}

async function createCodexDiagnostic(jobId, button) {
  if (!jobId) return;
  if (button.dataset.download) {
    location.href = button.dataset.download;
    return;
  }
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Generando...";
  try {
    const result = await api("/api/codex-diagnostic", {
      method: "POST",
      body: JSON.stringify({ job_id: jobId })
    });
    if (!result.ok) throw new Error(result.error || "No se pudo generar.");
    button.dataset.download = result.download_url;
    button.textContent = "Descargar";
    button.disabled = false;
    location.href = result.download_url;
  } catch (error) {
    button.textContent = "Error";
    button.title = error.message;
    setTimeout(() => {
      button.textContent = original;
      button.disabled = false;
    }, 2500);
  }
}

document.addEventListener("click", event => {
  const button = event.target.closest("[data-codex-job]");
  if (!button) return;
  createCodexDiagnostic(button.dataset.codexJob, button);
});

const routes = {
  reglas: showReglas,
  motor: showMotor,
  historial: showHistorial,
  revision: showRevision,
  informes: showInformes
};

tabs.forEach(btn => btn.addEventListener("click", () => {
  const view = btn.dataset.view;
  location.hash = view;
  routes[view]();
}));

window.addEventListener("hashchange", () => {
  const view = location.hash.replace("#", "") || "reglas";
  (routes[view] || showReglas)();
});

(routes[location.hash.replace("#", "")] || showReglas)().catch(error => {
  app.innerHTML = `<section class="panel"><h2>Error</h2><pre class="pre">${esc(error.message)}</pre></section>`;
});
