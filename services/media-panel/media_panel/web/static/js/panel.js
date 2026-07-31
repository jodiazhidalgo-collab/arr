const app = document.getElementById("app");
const title = document.getElementById("title");
const tabs = [...document.querySelectorAll(".tabs button")];

const PANEL_ROUTE_STORAGE_KEY = "arr-media-panel-route";
const LEGACY_RULE_SECTION_STORAGE_KEY = "arr-media-panel-rule-section";
let viewEpoch = 0;
const auxiliaryProfiles = {
  revision: storageGet("arr-media-panel-revision-profile", "movies") === "series" ? "series" : "movies",
  informes: storageGet("arr-media-panel-informes-profile", "movies") === "series" ? "series" : "movies"
};

function currentViewContext(view) {
  return Object.freeze({ view, epoch: viewEpoch, hash: location.hash });
}

function beginViewContext(view) {
  viewEpoch += 1;
  return currentViewContext(view);
}

function ensureViewContext(view, context) {
  return context?.view === view && Number.isInteger(context?.epoch)
    ? context
    : beginViewContext(view);
}

function isCurrentViewContext(context) {
  return Boolean(context)
    && context.epoch === viewEpoch
    && context.hash === location.hash
    && context.view === routeKeyFromHash();
}

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
          { type: "list", path: "video.idiomas_indeterminados_como_es", label: "Idiomas renombrados a ES" }
        ]
      },
      {
        title: "Correccion idioma video segun idioma audio",
        note: "Permite corregir la etiqueta del idioma del video si hay audio espanol valido y el idioma del video esta dentro de Idiomas corregibles.",
        controls: [
          { type: "boolean", path: "video.aceptar_por_audio_es", label: "Aceptar por audio ES" },
          { type: "list", path: "video.idiomas_corregibles_por_audio_es", label: "Idiomas corregibles" },
          { type: "text", path: "video.idioma_final_por_audio_es", label: "Idioma final por audio" }
        ]
      },
      {
        title: "Salida",
        note: "Etiqueta final del idioma del vídeo si es aceptado.",
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
  },
  vigilante: {
    title: "Vigilantes",
    help: "Finales de nombre que ARR ignora antes de crear un trabajo.",
    groups: [
      {
        title: "Archivos ignorados",
        note: "Si un archivo, incluso dentro de una carpeta, termina así, ARR ignora la carpeta completa.",
        controls: [
          { type: "list", path: "ignored_suffixes", label: "Extensiones o finales ignorados" }
        ]
      }
    ]
  }
};

const CLEANING_SECTIONS = Object.freeze(["entrada", "video", "audio", "subtitulos", "limpieza"]);
const SETTINGS_SECTIONS = Object.freeze(["trailers", "vigilantes"]);
const RULE_VIEW_CONFIG = Object.freeze({
  "limpieza-peliculas": Object.freeze({
    profile: "movies",
    label: "Limpieza películas",
    sections: CLEANING_SECTIONS,
    defaultSection: "entrada",
    sources: Object.freeze({
      rules: Object.freeze({ endpoint: "/api/movie-rules", label: "Motor de películas" })
    })
  }),
  "limpieza-series": Object.freeze({
    profile: "series",
    label: "Limpieza series",
    sections: CLEANING_SECTIONS,
    defaultSection: "entrada",
    sources: Object.freeze({
      rules: Object.freeze({ endpoint: "/api/series-rules", label: "Motor de series" })
    })
  }),
  ajustes: Object.freeze({
    profile: "settings",
    label: "Ajustes",
    sections: SETTINGS_SECTIONS,
    defaultSection: "trailers",
    sources: Object.freeze({
      trailers: Object.freeze({ endpoint: "/api/trailer-rules", label: "Trailers" }),
      watcherMovies: Object.freeze({ endpoint: "/api/watcher-rules/movies", label: "Vigilante de películas" }),
      watcherTv: Object.freeze({ endpoint: "/api/watcher-rules/tv", label: "Vigilante de series" })
    })
  })
});

function storageGet(key, fallback = null) {
  try {
    const value = localStorage.getItem(key);
    return value === null ? fallback : value;
  } catch (_error) {
    return fallback;
  }
}

function storageSet(key, value) {
  try { localStorage.setItem(key, value); } catch (_error) { return; }
}

function ruleSectionStorageKey(view) {
  return `arr-media-panel-section-${RULE_VIEW_CONFIG[view].profile}`;
}

function readStoredRuleSection(view) {
  const config = RULE_VIEW_CONFIG[view];
  const stored = storageGet(ruleSectionStorageKey(view), config.defaultSection);
  return config.sections.includes(stored) ? stored : config.defaultSection;
}

function createRuleViewState(view) {
  return {
    view,
    section: readStoredRuleSection(view),
    watcherProfile: storageGet("arr-media-panel-ajustes-vigilante", "movies") === "tv" ? "tv" : "movies",
    documents: {},
    drafts: {},
    dirty: {},
    loading: {},
    saving: {},
    requestEpoch: {},
    notice: {}
  };
}

const ruleViewStates = Object.fromEntries(Object.keys(RULE_VIEW_CONFIG).map(view => [view, createRuleViewState(view)]));

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
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : {};
  } catch (_error) {
    payload = null;
  }
  if (!response.ok) {
    const error = new Error(payload?.message || payload?.error || text || `Error HTTP ${response.status}`);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload || {};
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

async function showMotor(context) {
  context = ensureViewContext("motor", context);
  setActive("motor");
  title.textContent = "Estado del motor";
  app.innerHTML = `<section class="panel">Cargando motor...</section>`;
  const [status, jobs] = await Promise.all([api("/api/status"), api("/api/jobs")]);
  if (!isCurrentViewContext(context)) return;
  const orchOk = status.orchestrator?.status === "ok";
  const workerOk = status.media_worker?.status === "ok";
  const deps = status.orchestrator?.dependencies || {};
  const services = status.services || {};
  const latest = (jobs.jobs || []).slice(0, 8);
  app.innerHTML = `
    <section class="grid">
      ${serviceStatusCard("Orquestador", services.orchestrator, orchOk)}
      ${serviceStatusCard("Películas", services.movies, workerOk)}
      ${serviceStatusCard("Series", services.series, false)}
      ${serviceStatusCard("Trailers", services.trailers, workerOk)}
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

function serviceStatusCard(label, service, legacyOk = false) {
  const connected = typeof service?.connected === "boolean" ? service.connected : legacyOk;
  const message = service?.message || service?.status || (connected ? "activo" : "no conectado");
  return `<div class="card"><small>${esc(label)}</small><span class="metric">${connected ? "OK" : "—"}</span>${pill(message, connected ? "ok" : "warn")}</div>`;
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

async function showHistorial(context) {
  context = ensureViewContext("historial", context);
  setActive("historial");
  title.textContent = "Historial";
  app.innerHTML = `<section class="panel">Cargando historial...</section>`;
  const data = await api("/api/jobs");
  if (!isCurrentViewContext(context)) return;
  const jobs = data.jobs || [];
  app.innerHTML = `<section class="panel"><h2>Trabajos recientes</h2>${jobsTable(jobs)}</section>`;
}

function renderAuxiliaryProfileSelector(view, profile) {
  return `<div class="segmented-tabs auxiliary-profile-tabs" role="group" aria-label="Perfil de ${view === "revision" ? "revisión" : "informes"}">
    <button type="button" class="${profile === "movies" ? "active" : ""}" data-aux-profile="movies">Películas</button>
    <button type="button" class="${profile === "series" ? "active" : ""}" data-aux-profile="series">Series</button>
  </div>`;
}

function bindAuxiliaryProfileSelector(view, renderer) {
  document.querySelectorAll("[data-aux-profile]").forEach(button => button.addEventListener("click", () => {
    const profile = button.dataset.auxProfile === "series" ? "series" : "movies";
    if (profile === auxiliaryProfiles[view]) return;
    auxiliaryProfiles[view] = profile;
    storageSet(`arr-media-panel-${view}-profile`, profile);
    renderer();
  }));
}

async function showRevision(context) {
  context = ensureViewContext("revision", context);
  setActive("revision");
  title.textContent = "Revisión";
  const profile = auxiliaryProfiles.revision;
  app.innerHTML = `<section class="panel">Cargando revisión de ${profile === "series" ? "series" : "películas"}...</section>`;
  const data = await api(`/api/review?profile=${encodeURIComponent(profile)}`);
  if (!isCurrentViewContext(context)) return;
  const items = data.items || [];
  app.innerHTML = `<section class="panel">
    ${renderAuxiliaryProfileSelector("revision", profile)}
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
  bindAuxiliaryProfileSelector("revision", showRevision);
}

async function showInformes(context) {
  context = ensureViewContext("informes", context);
  setActive("informes");
  title.textContent = "Informes";
  const profile = auxiliaryProfiles.informes;
  app.innerHTML = `<section class="panel">Cargando informes de ${profile === "series" ? "series" : "películas"}...</section>`;
  const [data, codex] = await Promise.all([
    api(`/api/reports?profile=${encodeURIComponent(profile)}`),
    api("/api/codex-diagnostics")
  ]);
  if (!isCurrentViewContext(context)) return;
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
    ${renderAuxiliaryProfileSelector("informes", profile)}
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
    if (!isCurrentViewContext(context)) return;
    const box = document.getElementById("report-view");
    if (!box) return;
    box.style.display = "block";
    box.querySelector("pre").textContent = text;
  }));
  bindAuxiliaryProfileSelector("informes", showInformes);
}

function ruleSourceKey(view, state = ruleViewStates[view]) {
  if (view !== "ajustes") return "rules";
  if (state.section === "trailers") return "trailers";
  return state.watcherProfile === "tv" ? "watcherTv" : "watcherMovies";
}

function ruleSourceConfig(view, source = ruleSourceKey(view)) {
  return RULE_VIEW_CONFIG[view].sources[source];
}

function isRuleSourceActive(view, source) {
  return routeKeyFromHash() === view && ruleSourceKey(view) === source;
}

function ruleDocumentEditable(documentState) {
  return Boolean(documentState)
    && documentState.ok !== false
    && documentState.connected !== false
    && documentState.editable !== false;
}

function ruleDocumentStatus(documentState, sourceConfig) {
  if (!documentState) return `Cargando ${sourceConfig.label.toLowerCase()}…`;
  if (documentState.connected === false || documentState.editable === false) {
    return documentState.message || `${sourceConfig.label} no conectado`;
  }
  if (documentState.ok === false) {
    return documentState.message || documentState.error || `No se pudo cargar ${sourceConfig.label.toLowerCase()}.`;
  }
  const fingerprint = String(documentState.fingerprint || "").slice(0, 12);
  return `${sourceConfig.label} activo para trabajos nuevos${fingerprint ? ` · Huella ${fingerprint}` : ""}`;
}

function renderRuleProfile(view) {
  if (routeKeyFromHash() !== view) return;
  const config = RULE_VIEW_CONFIG[view];
  const state = ruleViewStates[view];
  const source = ruleSourceKey(view, state);
  const sourceConfig = ruleSourceConfig(view, source);
  const documentState = state.documents[source];
  const draft = state.drafts[source];
  if (!documentState || !draft) {
    app.innerHTML = `<section class="panel">Cargando ${esc(sourceConfig.label.toLowerCase())}...</section>`;
    return;
  }
  const editable = ruleDocumentEditable(documentState);
  const sectionKey = state.section === "vigilantes" ? "vigilante" : state.section;
  const section = RULE_SECTIONS[sectionKey];
  const dirty = Boolean(state.dirty[source]);
  const saving = Boolean(state.saving[source]);
  const statusText = state.notice[source] || ruleDocumentStatus(documentState, sourceConfig);
  const sectionButtons = config.sections.map(key =>
    `<button class="${key === state.section ? "active" : ""}" data-rule-section="${esc(key)}">${esc(RULE_SECTIONS[key === "vigilantes" ? "vigilante" : key].title)}</button>`
  ).join("");
  const watcherSelector = view === "ajustes" && state.section === "vigilantes"
    ? `<div class="segmented-tabs rule-profile-tabs" role="group" aria-label="Perfil del Vigilante ARR">
        <button type="button" class="${state.watcherProfile === "movies" ? "active" : ""}" data-watcher-profile="movies">Películas</button>
        <button type="button" class="${state.watcherProfile === "tv" ? "active" : ""}" data-watcher-profile="tv">Series</button>
      </div>`
    : "";
  const lockedNotice = editable ? "" : `<div class="locked-notice" role="status">
    <span class="pill warn">Solo lectura</span>
    <strong>${esc(documentState.message || `${sourceConfig.label} no conectado`)}</strong>
    <span>La configuración se muestra completa, pero no se enviará ningún cambio.</span>
  </div>`;

  app.innerHTML = `<section class="split rule-profile-shell" data-rule-view="${esc(view)}" data-rule-source="${esc(source)}">
    <aside class="side">${sectionButtons}</aside>
    <div class="rules-work panel">
      <div class="toolbar">
        <div>
          <span class="identity-kicker">${esc(config.label)}</span>
          <h2>${esc(section.title)}</h2>
          <div class="muted">${esc(section.help)}</div>
        </div>
        <div class="toolbar-actions">
          <button class="btn ghost" id="reload-rules-profile">Recargar</button>
          <button class="btn primary" id="save-rules-profile" data-tooltip="${esc(sourceConfig.endpoint)}" ${!editable || !dirty || saving ? "disabled" : ""}>${saving ? "Guardando…" : "Guardar reglas"}</button>
        </div>
      </div>
      ${watcherSelector}
      <div id="rules-status" class="status ${editable ? "" : "warn"}" role="status" aria-live="polite">${esc(statusText)}</div>
      ${lockedNotice}
      <div id="rules-editor" ${editable ? "" : 'class="rules-editor-locked" aria-disabled="true"'}>
        ${section.groups.map(group => renderRuleProfileGroup(group, draft, editable)).join("")}
      </div>
    </div>
  </section>`;

  document.querySelectorAll("[data-rule-section]").forEach(button => button.addEventListener("click", () => {
    const sectionTarget = button.dataset.ruleSection;
    state.section = sectionTarget;
    storageSet(ruleSectionStorageKey(view), sectionTarget);
    const targetHash = `#${view}/${sectionTarget}`;
    if (location.hash === targetHash) renderRuleProfile(view);
    else location.hash = targetHash;
  }));
  document.querySelectorAll("[data-watcher-profile]").forEach(button => button.addEventListener("click", () => {
    state.watcherProfile = button.dataset.watcherProfile === "tv" ? "tv" : "movies";
    storageSet("arr-media-panel-ajustes-vigilante", state.watcherProfile);
    const nextSource = ruleSourceKey(view, state);
    if (state.documents[nextSource]) renderRuleProfile(view);
    else loadRuleSource(view, nextSource);
  }));
  document.querySelectorAll("[data-rule-path]").forEach(input => {
    const update = () => updateRuleDraftFromInput(view, source, input);
    input.addEventListener("input", update);
    input.addEventListener("change", update);
  });
  document.getElementById("reload-rules-profile")?.addEventListener("click", () => reloadRuleSource(view, source));
  document.getElementById("save-rules-profile")?.addEventListener("click", () => saveRuleSource(view, source));
}

function renderRuleProfileGroup(group, draft, editable) {
  return `<div class="rule-group">
    <h3>${esc(group.title)}</h3>
    <p>${esc(group.note || "")}</p>
    ${group.controls.map(control => renderRuleProfileControl(control, draft, editable)).join("")}
  </div>`;
}

function renderRuleProfileControl(control, draft, editable) {
  const value = getPath(draft, control.path);
  const id = `field-${control.path.replace(/[^a-z0-9]+/gi, "-")}`;
  const disabled = editable ? "" : "disabled";
  const hint = control.suffix ? `<span class="hint">${esc(control.suffix)}</span>` : "";
  const formatHint = control.format ? `<span class="hint">${esc(control.format)}</span>` : "";
  let input = "";
  if (control.type === "boolean") {
    input = `<label class="toggle"><input id="${id}" data-rule-path="${esc(control.path)}" data-rule-type="boolean" type="checkbox" ${value ? "checked" : ""} ${disabled}> Activo</label>`;
  } else if (control.type === "number") {
    input = `<input id="${id}" data-rule-path="${esc(control.path)}" data-rule-type="number" type="number" value="${esc(value ?? "")}" min="${esc(control.min ?? "")}" max="${esc(control.max ?? "")}" step="${esc(control.step ?? 1)}" ${disabled}>${hint}`;
  } else if (control.type === "list") {
    input = `<textarea id="${id}" data-rule-path="${esc(control.path)}" data-rule-type="list" ${disabled}>${esc((value || []).join("\n"))}</textarea><span class="hint">Una entrada por línea.</span>${formatHint}`;
  } else if (control.type === "kv-number" || control.type === "kv-text") {
    input = `<textarea id="${id}" data-rule-path="${esc(control.path)}" data-rule-type="${control.type}" ${disabled}>${esc(Object.entries(value || {}).map(([key, item]) => `${key}: ${item}`).join("\n"))}</textarea><span class="hint">Formato: clave: valor</span>`;
  } else if (control.type === "select") {
    input = `<select id="${id}" data-rule-path="${esc(control.path)}" data-rule-type="text" ${disabled}>
      ${(control.options || []).map(option => `<option value="${esc(option.value)}" ${option.value === value ? "selected" : ""}>${esc(option.label)}</option>`).join("")}
    </select>`;
  } else {
    input = `<input id="${id}" data-rule-path="${esc(control.path)}" data-rule-type="text" type="text" value="${esc(value ?? "")}" ${disabled}>`;
  }
  return `<div class="field"><label for="${id}">${esc(control.label)}</label><div>${input}</div></div>`;
}

function inputRuleValue(input) {
  const type = input.dataset.ruleType;
  if (type === "boolean") return input.checked;
  if (type === "number") return Number(input.value);
  if (type === "list") {
    return input.value.split(/\r?\n/).map(item => item.trim()).filter((item, index) =>
      Boolean(item) || (input.dataset.rulePath === "video.idiomas_indeterminados_como_es" && index === 0)
    );
  }
  if (type === "kv-number" || type === "kv-text") {
    const value = {};
    input.value.split(/\r?\n/).forEach(line => {
      const separator = line.indexOf(":");
      if (separator <= 0) return;
      const key = line.slice(0, separator).trim();
      const raw = line.slice(separator + 1).trim();
      if (key) value[key] = type === "kv-number" ? Number(raw) : raw;
    });
    return value;
  }
  return input.value;
}

function updateRuleDraftFromInput(view, source, input) {
  const state = ruleViewStates[view];
  const documentState = state.documents[source];
  if (!ruleDocumentEditable(documentState) || !state.drafts[source]) return;
  setPath(state.drafts[source], input.dataset.rulePath, inputRuleValue(input));
  state.dirty[source] = JSON.stringify(state.drafts[source]) !== JSON.stringify(documentState.rules);
  state.notice[source] = state.dirty[source] ? "Cambios sin guardar." : "Sin cambios pendientes.";
  const status = document.getElementById("rules-status");
  if (status && isRuleSourceActive(view, source)) status.textContent = state.notice[source];
  const save = document.getElementById("save-rules-profile");
  if (save && isRuleSourceActive(view, source)) save.disabled = !state.dirty[source];
}

async function loadRuleSource(view, source, { replace = false } = {}) {
  const state = ruleViewStates[view];
  const sourceConfig = ruleSourceConfig(view, source);
  if (!state || !sourceConfig || state.loading[source]) return;
  if (state.documents[source] && !replace) {
    if (isRuleSourceActive(view, source)) renderRuleProfile(view);
    return;
  }
  const requestEpoch = Number(state.requestEpoch[source] || 0) + 1;
  state.requestEpoch[source] = requestEpoch;
  state.loading[source] = true;
  if (isRuleSourceActive(view, source)) {
    app.innerHTML = `<section class="panel">Cargando ${esc(sourceConfig.label.toLowerCase())}...</section>`;
  }
  try {
    const payload = await api(sourceConfig.endpoint);
    if (state.requestEpoch[source] !== requestEpoch) return;
    if (!payload || typeof payload !== "object" || Array.isArray(payload) || typeof payload.rules !== "object") {
      throw new Error("El servidor no devolvió el contrato completo de reglas.");
    }
    state.documents[source] = payload;
    state.drafts[source] = clone(payload.rules);
    state.dirty[source] = false;
    state.notice[source] = replace ? "Configuración recargada desde el motor." : "";
    if (isRuleSourceActive(view, source)) renderRuleProfile(view);
  } catch (error) {
    if (state.requestEpoch[source] !== requestEpoch) return;
    state.notice[source] = `Error cargando: ${error.message}`;
    if (isRuleSourceActive(view, source)) {
      app.innerHTML = `<section class="panel identity-load-error"><span class="pill bad">No disponible</span><div><h2>No se pudo cargar ${esc(sourceConfig.label.toLowerCase())}</h2><p>${esc(error.message)}</p></div><button type="button" class="btn primary" id="rules-load-retry">Reintentar</button></section>`;
      document.getElementById("rules-load-retry")?.addEventListener("click", () => loadRuleSource(view, source, { replace: true }));
    }
  } finally {
    if (state.requestEpoch[source] === requestEpoch) state.loading[source] = false;
  }
}

function reloadRuleSource(view, source) {
  const state = ruleViewStates[view];
  if (state.dirty[source] && !window.confirm("Recargar descartará los cambios sin guardar. ¿Continuar?")) return;
  return loadRuleSource(view, source, { replace: true });
}

async function saveRuleSource(view, source) {
  const state = ruleViewStates[view];
  const documentState = state.documents[source];
  const sourceConfig = ruleSourceConfig(view, source);
  if (!ruleDocumentEditable(documentState) || !state.dirty[source] || state.saving[source]) return;
  const submittedDraft = clone(state.drafts[source]);
  state.saving[source] = true;
  if (isRuleSourceActive(view, source)) renderRuleProfile(view);
  try {
    const savedState = await api(sourceConfig.endpoint, {
      method: "POST",
      body: JSON.stringify({ rules: submittedDraft, expected_fingerprint: documentState.fingerprint ?? null })
    });
    if (!savedState || savedState.ok === false || typeof savedState.rules !== "object") {
      throw new Error(savedState?.message || savedState?.error || "El motor no confirmó las reglas guardadas.");
    }
    const changedWhileSaving = JSON.stringify(state.drafts[source]) !== JSON.stringify(submittedDraft);
    const currentDraft = state.drafts[source];
    state.documents[source] = savedState;
    state.drafts[source] = changedWhileSaving ? currentDraft : clone(savedState.rules);
    state.dirty[source] = changedWhileSaving
      && JSON.stringify(currentDraft) !== JSON.stringify(savedState.rules);
    state.notice[source] = changedWhileSaving
      ? "La revisión enviada se guardó; los cambios posteriores siguen en el borrador."
      : "Reglas guardadas y activas para trabajos nuevos.";
  } catch (error) {
    state.notice[source] = error.status === 409
      ? `Conflicto al guardar: ${error.message} Recarga y vuelve a intentarlo.`
      : `Error guardando: ${error.message}`;
  } finally {
    state.saving[source] = false;
    if (isRuleSourceActive(view, source)) renderRuleProfile(view);
  }
}

async function showRuleProfile(view, context) {
  context = ensureViewContext(view, context);
  const state = ruleViewStates[view];
  const config = RULE_VIEW_CONFIG[view];
  setActive(view);
  title.textContent = config.label;
  const route = canonicalRouteFromHash(location.hash);
  if (route?.view === view && config.sections.includes(route.section)) state.section = route.section;
  storageSet(ruleSectionStorageKey(view), state.section);
  const source = ruleSourceKey(view, state);
  if (state.documents[source]) renderRuleProfile(view);
  else await loadRuleSource(view, source);
  if (!isCurrentViewContext(context)) return;
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
  identidad: context => window.ArrIdentityUI.show(context),
  "limpieza-peliculas": context => showRuleProfile("limpieza-peliculas", context),
  "limpieza-series": context => showRuleProfile("limpieza-series", context),
  ajustes: context => showRuleProfile("ajustes", context),
  motor: showMotor,
  historial: showHistorial,
  revision: showRevision,
  informes: showInformes
};

function exactCanonicalRoute(hash) {
  const identity = typeof window.ArrIdentityUI?.identityRouteFromHash === "function"
    ? window.ArrIdentityUI.identityRouteFromHash(hash)
    : null;
  if (identity) return { ...identity, view: "identidad" };
  const cleaning = String(hash || "").match(/^#(limpieza-peliculas|limpieza-series)\/(entrada|video|audio|subtitulos|limpieza)$/);
  if (cleaning) return { view: cleaning[1], section: cleaning[2], hash: `#${cleaning[1]}/${cleaning[2]}` };
  const settings = String(hash || "").match(/^#ajustes\/(trailers|vigilantes)$/);
  if (settings) return { view: "ajustes", section: settings[1], hash: `#ajustes/${settings[1]}` };
  const simple = String(hash || "").match(/^#(motor|historial|revision|informes)$/);
  if (simple) return { view: simple[1], hash: `#${simple[1]}` };
  return null;
}

function canonicalRouteFromHash(hash = location.hash) {
  const exact = exactCanonicalRoute(hash);
  if (exact) return exact;

  const identityTarget = typeof window.ArrIdentityUI?.resolveTarget === "function"
    ? window.ArrIdentityUI.resolveTarget(hash)
    : null;
  if (identityTarget) return { ...identityTarget, view: "identidad" };

  const legacyRules = String(hash || "").match(/^#reglas(?:\/(entrada|video|audio|subtitulos|limpieza|trailers|vigilante|vigilantes))?$/);
  if (legacyRules) {
    const legacySection = legacyRules[1] || storageGet(LEGACY_RULE_SECTION_STORAGE_KEY, "entrada");
    if (legacySection === "trailers") return { view: "ajustes", section: "trailers", hash: "#ajustes/trailers", legacy: true };
    if (["vigilante", "vigilantes"].includes(legacySection)) return { view: "ajustes", section: "vigilantes", hash: "#ajustes/vigilantes", legacy: true };
    const section = CLEANING_SECTIONS.includes(legacySection) ? legacySection : "entrada";
    return { view: "limpieza-peliculas", section, hash: `#limpieza-peliculas/${section}`, legacy: true };
  }

  const partialRules = String(hash || "").match(/^#(limpieza-peliculas|limpieza-series|ajustes)$/);
  if (partialRules) {
    const view = partialRules[1];
    const section = readStoredRuleSection(view);
    return { view, section, hash: `#${view}/${section}`, partial: true };
  }

  const stored = exactCanonicalRoute(storageGet(PANEL_ROUTE_STORAGE_KEY, ""));
  if (stored) return stored;
  return { view: "identidad", profile: "common", section: "parser", hash: "#identidad/comun/parser", fallback: true };
}

function normalizeLocationRoute() {
  const route = canonicalRouteFromHash(location.hash);
  if (location.hash !== route.hash) history.replaceState(null, "", route.hash);
  storageSet(PANEL_ROUTE_STORAGE_KEY, route.hash);
  return route;
}

function routeFromHash() {
  return exactCanonicalRoute(location.hash)?.view || canonicalRouteFromHash(location.hash).view;
}

function routeKeyFromHash() {
  return routeFromHash();
}

function tabTarget(view) {
  if (routeKeyFromHash() === view) return location.hash;
  if (view === "identidad" && typeof window.ArrIdentityUI?.resolveTarget === "function") {
    return window.ArrIdentityUI.resolveTarget("#identidad").hash;
  }
  if (view === "identidad") return "#identidad/comun/parser";
  if (RULE_VIEW_CONFIG[view]) return `#${view}/${readStoredRuleSection(view)}`;
  return `#${view}`;
}

tabs.forEach(button => button.addEventListener("click", () => {
  const view = button.dataset.view;
  const target = tabTarget(view);
  if (location.hash === target) dispatchRoute();
  else location.hash = target;
}));

function dispatchRoute() {
  const route = normalizeLocationRoute();
  const routeKey = route.view;
  const context = beginViewContext(routeKey);
  return Promise.resolve(routes[routeKey](context)).catch(error => {
    if (!isCurrentViewContext(context)) return;
    app.innerHTML = `<section class="panel"><h2>Error</h2><pre class="pre">${esc(error.message)}</pre></section>`;
  });
}

window.addEventListener("hashchange", () => {
  dispatchRoute();
});

window.addEventListener("beforeunload", event => {
  const dirty = Object.values(ruleViewStates).some(state => Object.values(state.dirty).some(Boolean));
  if (!dirty) return;
  event.preventDefault();
  event.returnValue = "";
});

dispatchRoute();
