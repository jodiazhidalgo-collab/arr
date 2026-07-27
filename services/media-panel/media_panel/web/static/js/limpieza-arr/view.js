(function () {
  "use strict";

  const ui = window.ArrIdentityUI;
  if (!ui) return;

  const API_ROOT = "/api/identity-rules";
  const SECTION_STORAGE_KEY = "arr-identity-section";
  const MAX_IMPORT_BYTES = 4 * 1024 * 1024;

  function isObject(value) {
    return Boolean(value) && typeof value === "object" && !Array.isArray(value);
  }

  function sameValue(left, right) {
    try { return JSON.stringify(left) === JSON.stringify(right); } catch (_error) { return false; }
  }

  function validRulesDocument(payload) {
    return isObject(payload)
      && payload.ok === true
      && isObject(payload.rules)
      && isObject(payload.rules.parser)
      && isObject(payload.rules.resolver)
      && isObject(payload.defaults)
      && isObject(payload.defaults.parser)
      && isObject(payload.defaults.resolver)
      && isObject(payload.schema)
      && isObject(payload.schema.parser)
      && isObject(payload.schema.resolver)
      && Number.isInteger(payload.revision)
      && payload.revision >= 0;
  }

  function validCachePayload(payload) {
    return isObject(payload)
      && payload.ok === true
      && isObject(payload.cache_status)
      && Number.isFinite(Number(payload.deleted));
  }

  function activeSectionFromState() {
    const fromHash = ui.sectionFromHash();
    if (location.hash.startsWith("#limpieza-arr/")) return fromHash;
    const stored = ui.storageGet(SECTION_STORAGE_KEY);
    if (stored === "parser" || stored === "resolver") return stored;
    return ui.state.section === "resolver" ? "resolver" : "parser";
  }

  function rememberSection(section) {
    ui.storageSet(SECTION_STORAGE_KEY, section);
  }

  function activateMainTab() {
    document.querySelectorAll(".tabs [data-view]").forEach(button => {
      button.classList.toggle("active", button.dataset.view === "limpieza-arr");
    });
    const title = document.getElementById("title");
    if (title) title.textContent = "Limpieza ARR";
  }

  function setNotice(message, tone = "info") {
    ui.state.notice = { message, tone };
    if (!ui.isActiveView()) return;
    const box = document.getElementById("identity-status");
    if (!box) return;
    box.className = `status identity-status ${tone}`;
    box.textContent = message;
  }

  if (!ui._noticeWrapped) {
    ui.status = setNotice;
    ui._noticeWrapped = true;
  }

  function confirmDraftLoss(message) {
    return !ui.state.dirty || window.confirm(message);
  }

  function cacheSummary(cache) {
    if (!cache || cache.available === false) return "No disponible";
    const active = cache.active ?? cache.valid ?? cache.entries ?? cache.total;
    const total = cache.total ?? cache.entries;
    if (active !== undefined && total !== undefined && active !== total) return `${active} activas de ${total}`;
    if (total !== undefined) return `${total} entradas`;
    return "Disponible";
  }

  function renderMetadata() {
    const documentState = ui.state.document || {};
    const rules = ui.state.draft || {};
    const fingerprint = String(documentState.fingerprint || "");
    return `<section class="identity-meta-card">
      <div class="identity-card-heading"><div><span class="identity-kicker">Configuración activa</span><h3>Metadatos</h3></div>
        <span class="pill ${ui.state.dirty ? "warn" : "ok"}">${ui.state.dirty ? "Borrador" : "Sin cambios"}</span>
      </div>
      <dl class="identity-meta-list">
        <div><dt>Revisión</dt><dd>${ui.esc(documentState.revision ?? 0)}</dd></div>
        <div><dt>Guardada</dt><dd>${ui.esc(ui.formatDate(documentState.saved_at))}</dd></div>
        <div><dt>Esquema</dt><dd>v${ui.esc(rules.schema_version ?? "-")}</dd></div>
        <div><dt>Huella</dt><dd><code title="${ui.esc(fingerprint)}">${ui.esc(fingerprint ? `${fingerprint.slice(0, 22)}…` : "-")}</code></dd></div>
        <div><dt>Origen</dt><dd><code>${ui.esc(documentState.rules_path || "-")}</code></dd></div>
      </dl>
    </section>`;
  }

  function renderHistory() {
    const history = [...(ui.state.document?.history || [])].reverse();
    const rows = history.map(item => `<li>
      <div><strong>Revisión ${ui.esc(item.revision ?? "-")}</strong>
        <small>${item.action === "reset" ? "Restablecida" : "Guardada"} · ${ui.esc(ui.formatDate(item.saved_at))}</small></div>
      <code title="${ui.esc(item.fingerprint || "")}">${ui.esc(String(item.fingerprint || "-").slice(0, 15))}${item.fingerprint ? "…" : ""}</code>
    </li>`).join("");
    return `<section class="identity-meta-card identity-history-card">
      <div class="identity-card-heading"><div><span class="identity-kicker">Últimos cambios</span><h3>Historial</h3></div>
        <span class="pill info">${history.length}</span></div>
      ${rows ? `<ol class="identity-history">${rows}</ol>` : `<div class="identity-empty-small">Todavía no hay guardados.</div>`}
    </section>`;
  }

  function renderCacheCard() {
    if (ui.state.section !== "resolver") return "";
    const cache = ui.state.document?.cache_status || {};
    return `<section class="identity-meta-card identity-cache-card">
      <div class="identity-card-heading"><div><span class="identity-kicker">Resolver TMDb</span><h3>Caché</h3></div>
        <span class="pill ${cache.available === false ? "warn" : "info"}">${ui.esc(cacheSummary(cache))}</span></div>
      <p>Las reglas no cambian al vaciarla. Las próximas resoluciones volverán a consultar TMDb.</p>
    </section>`;
  }

  function renderToolbar() {
    const saving = Boolean(ui.state.saving);
    return `<div class="identity-toolbar toolbar">
      <div id="identity-status" class="status identity-status ${ui.esc(ui.state.notice?.tone || "info")}" role="status" aria-live="polite">
        ${ui.esc(ui.state.notice?.message || "Configuración preparada.")}
      </div>
      <div class="toolbar-actions identity-toolbar-actions">
        <button type="button" class="btn ghost" id="identity-reload">Recargar</button>
        <button type="button" class="btn ghost" id="identity-reset">Restablecer</button>
        <button type="button" class="btn primary" id="identity-save" data-tooltip="Orquestador /api/identity-rules" ${!ui.state.dirty || saving ? "disabled" : ""} ${!ui.state.dirty && !saving ? 'data-idle-disabled="true"' : ""}>${saving ? "Guardando…" : "Guardar"}</button>
        <button type="button" class="btn ghost" id="identity-export">Exportar</button>
        <button type="button" class="btn ghost" id="identity-import">Importar</button>
        ${ui.state.section === "resolver" ? `<button type="button" class="btn ghost danger" id="identity-clear-cache" ${ui.state.cacheClearing ? "disabled" : ""}>${ui.state.cacheClearing ? "Limpiando…" : "Limpiar caché"}</button>` : ""}
      </div>
    </div>`;
  }

  function renderLoading() {
    if (!ui.isActiveView()) return;
    const app = document.getElementById("app");
    if (!app) return;
    app.innerHTML = `<section class="panel identity-loading"><span class="identity-spinner" aria-hidden="true"></span><div><h2>Limpieza ARR</h2><p>Cargando reglas del motor…</p></div></section>`;
  }

  function renderLoadError(error) {
    if (!ui.isActiveView()) return;
    const app = document.getElementById("app");
    if (!app) return;
    app.innerHTML = `<section class="panel identity-load-error">
      <span class="pill bad">No disponible</span><div><h2>No se pudieron cargar las reglas</h2><p>${ui.esc(error.message || error)}</p></div>
      <button type="button" class="btn primary" id="identity-load-retry">Reintentar</button>
    </section>`;
    document.getElementById("identity-load-retry")?.addEventListener("click", () => ui.loadRules({ replace: true }));
  }

  ui.render = function () {
    if (!ui.isActiveView()) return;
    const app = document.getElementById("app");
    if (!app || !ui.state.document || !ui.state.draft) return;
    const section = ui.state.section === "resolver" ? "resolver" : "parser";
    const sectionSchema = ui.state.document.schema?.[section] || { title: section, groups: [] };
    const dirtyText = ui.state.dirty ? "Cambios sin guardar" : "Configuración guardada";

    app.innerHTML = `<section class="identity-shell" data-identity-section="${section}">
      <header class="identity-hero">
        <div><span class="identity-kicker">Nombre sucio → identidad segura</span>
          <h2>${section === "parser" ? "Limpiador / Parser" : "Resolver TMDb"}</h2>
          <p>${section === "parser"
            ? "Limpia el release, extrae título, año, temporada y episodios antes de consultar servicios externos."
            : "Construye candidatos, puntúa las coincidencias y decide cuándo una identidad es suficientemente segura."}</p>
        </div>
        <div class="identity-hero-state"><span class="pill ${ui.state.dirty ? "warn" : "ok"}">${dirtyText}</span><small>${ui.esc(ui.activeMetadata())}</small></div>
      </header>

      <nav class="identity-subtabs" role="tablist" aria-label="Configuración de Limpieza ARR">
        <button id="identity-tab-parser" type="button" role="tab" aria-selected="${section === "parser"}" aria-controls="identity-panel-parser" tabindex="${section === "parser" ? "0" : "-1"}" class="${section === "parser" ? "active" : ""}" data-identity-section="parser">
          <span>1</span><div><strong>Parser</strong><small>Limpieza y lectura</small></div>
        </button>
        <button id="identity-tab-resolver" type="button" role="tab" aria-selected="${section === "resolver"}" aria-controls="identity-panel-resolver" tabindex="${section === "resolver" ? "0" : "-1"}" class="${section === "resolver" ? "active" : ""}" data-identity-section="resolver">
          <span>2</span><div><strong>Resolver TMDb</strong><small>Candidatos y puntuación</small></div>
        </button>
      </nav>

      <div id="identity-panel-${section}" class="identity-tabpanel" role="tabpanel" aria-labelledby="identity-tab-${section}" tabindex="0">
        ${renderToolbar()}

        <div class="identity-workspace">
          <main class="identity-editor">
            ${ui.renderTester(section)}
            <section class="identity-schema-heading">
              <div><span class="identity-kicker">Controles del motor</span><h3>${ui.esc(sectionSchema.title || "Reglas")}</h3></div>
              <span class="pill info">Esquema dinámico</span>
            </section>
            <div class="identity-groups">${ui.renderGroups(sectionSchema) || `<div class="empty">No hay controles para esta sección.</div>`}</div>
          </main>
          <aside class="identity-sidebar">${renderMetadata()}${renderCacheCard()}${renderHistory()}</aside>
        </div>
      </div>
      <div id="identity-panel-${section === "parser" ? "resolver" : "parser"}" class="identity-tabpanel" role="tabpanel" aria-labelledby="identity-tab-${section === "parser" ? "resolver" : "parser"}" hidden></div>

      <input id="identity-import-file" type="file" accept="application/json,.json" hidden>
    </section>`;
    ui.bindView();
    ui.bindControls();
    ui.bindTester();
    if (ui.state.focusTabAfterRender) {
      ui.state.focusTabAfterRender = false;
      document.getElementById(`identity-tab-${section}`)?.focus();
    }
  };

  ui.bindView = function () {
    document.querySelectorAll("[data-identity-section]").forEach(button => {
      if (!button.matches("button")) return;
      button.addEventListener("click", () => ui.switchSection(button.dataset.identitySection, { focusTab: true }));
      button.addEventListener("keydown", event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const section = event.key === "ArrowLeft" || event.key === "Home" ? "parser" : "resolver";
        ui.switchSection(section, { focusTab: true });
      });
    });
    document.getElementById("identity-reload")?.addEventListener("click", ui.reloadRules);
    document.getElementById("identity-reset")?.addEventListener("click", ui.resetDraft);
    document.getElementById("identity-save")?.addEventListener("click", ui.saveRules);
    document.getElementById("identity-export")?.addEventListener("click", ui.exportRules);
    document.getElementById("identity-import")?.addEventListener("click", ui.openImport);
    document.getElementById("identity-import-file")?.addEventListener("change", ui.importRules);
    document.getElementById("identity-clear-cache")?.addEventListener("click", ui.clearResolverCache);
  };

  ui.switchSection = function (section, { focusTab = false } = {}) {
    if (!['parser', 'resolver'].includes(section)) return;
    ui.storeOpenGroups?.();
    ui.state.section = section;
    ui.state.focusTabAfterRender = focusTab;
    rememberSection(section);
    const nextHash = `#limpieza-arr/${section}`;
    if (location.hash !== nextHash) location.hash = nextHash;
    else ui.render();
  };

  ui.loadRules = async function ({ replace = false } = {}) {
    if (ui.state.loading) return;
    const hadDocument = Boolean(ui.state.document && ui.state.draft);
    ui.state.loading = true;
    if (!hadDocument) renderLoading();
    try {
      const payload = await ui.api(API_ROOT);
      if (!validRulesDocument(payload)) {
        throw new Error("El motor no devolvió el contrato completo de reglas.");
      }
      ui.state.document = payload;
      ui.state.draft = ui.clone(payload.rules);
      ui.state.dirty = false;
      ui.invalidateAllTestResults({ updateDom: false });
      ui.state.notice = payload.repair_required
        ? {
            message: "La configuración guardada no es válida. El motor usa valores seguros; pulsa Restablecer y después Guardar para repararla.",
            tone: "warn"
          }
        : { message: replace ? "Configuración recargada desde el motor." : "Configuración cargada.", tone: "ok" };
      if (ui.isActiveView()) ui.render();
    } catch (error) {
      if (hadDocument) {
        if (ui.isActiveView()) ui.render();
        setNotice(`No se pudo recargar; el borrador se conserva: ${error.message}`, "bad");
      } else if (ui.isActiveView()) {
        renderLoadError(error);
      }
    } finally {
      ui.state.loading = false;
    }
  };

  ui.reloadRules = function () {
    if (!confirmDraftLoss("Recargar descartará el borrador sin guardar. ¿Continuar?")) return;
    return ui.loadRules({ replace: true });
  };

  ui.resetDraft = function () {
    if (!confirmDraftLoss("Restablecer sustituirá el borrador actual por los valores de fábrica. ¿Continuar?")) return;
    ui.state.draft = ui.clone(ui.state.document.defaults);
    ui.state.dirty = !sameValue(ui.state.draft, ui.state.document.rules)
      || Boolean(ui.state.document.repair_required);
    ui.invalidateAllTestResults({ updateDom: false });
    ui.render();
    document.getElementById("identity-reset")?.focus();
    setNotice(ui.state.dirty
      ? "Valores de fábrica cargados en el borrador. Pulsa Guardar para aplicarlos."
      : "La configuración activa ya coincide con los valores de fábrica.", ui.state.dirty ? "warn" : "ok");
  };

  ui.saveRules = async function () {
    if (!ui.state.dirty || ui.state.saving) return;
    const submittedDraft = ui.clone(ui.state.draft);
    ui.state.saving = true;
    ui.render();
    try {
      const payload = await ui.api(API_ROOT, {
        method: "POST",
        body: JSON.stringify({
          rules: submittedDraft,
          expected_revision: Number(ui.state.document.revision || 0)
        })
      });
      if (!validRulesDocument(payload)) {
        throw new Error("El motor confirmó HTTP 200 sin el contrato completo de reglas.");
      }
      const changedWhileSaving = !sameValue(ui.state.draft, submittedDraft);
      const currentDraft = ui.state.draft;
      ui.state.document = payload;
      ui.state.draft = changedWhileSaving ? currentDraft : ui.clone(payload.rules);
      ui.state.dirty = changedWhileSaving && !sameValue(currentDraft, payload.rules);
      ui.invalidateAllTestResults({ updateDom: false });
      if (ui.isActiveView()) ui.render();
      setNotice(changedWhileSaving
        ? "La revisión enviada se guardó; los cambios hechos durante el guardado siguen en el borrador."
        : payload.saved === false
          ? "No había cambios nuevos que guardar."
          : "Configuración guardada, versionada y activa para trabajos nuevos.", changedWhileSaving ? "warn" : "ok");
    } catch (error) {
      if (ui.isActiveView()) ui.render();
      const conflict = error.status === 409 || error.payload?.error === "revision_conflict";
      setNotice(conflict
        ? "Otra ventana guardó una revisión nueva. Tu borrador se conserva: expórtalo o recarga antes de reintentar."
        : `No se pudo guardar; el borrador se conserva: ${error.message}`, "bad");
    } finally {
      ui.state.saving = false;
      const save = ui.isActiveView() ? document.getElementById("identity-save") : null;
      if (save) {
        save.disabled = !ui.state.dirty;
        save.toggleAttribute("data-idle-disabled", !ui.state.dirty);
        save.textContent = "Guardar";
      }
    }
  };

  ui.exportRules = function () {
    const payload = {
      exported_at: new Date().toISOString(),
      revision: ui.state.document.revision ?? 0,
      fingerprint: ui.state.document.fingerprint || null,
      rules: ui.clone(ui.state.draft)
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    link.href = url;
    link.download = `arr-identidad-rev-${payload.revision}-${stamp}.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    setNotice("Borrador exportado. No se ha guardado ningún cambio.", "ok");
  };

  ui.openImport = function () {
    if (!confirmDraftLoss("Importar sustituirá el borrador sin guardar. ¿Continuar?")) return;
    document.getElementById("identity-import-file")?.click();
  };

  ui.importRules = async function (event) {
    const input = event.currentTarget;
    const file = input.files?.[0];
    if (!file) return;
    try {
      if (file.size > MAX_IMPORT_BYTES) throw new Error("El JSON supera el límite de 4 MB.");
      const parsed = JSON.parse(await file.text());
      const rules = isObject(parsed?.rules) ? parsed.rules : parsed;
      if (!isObject(rules) || !isObject(rules.parser) || !isObject(rules.resolver)) {
        throw new Error("El archivo no contiene reglas de Parser y Resolver.");
      }
      ui.state.draft = ui.clone(rules);
      ui.state.dirty = !sameValue(ui.state.draft, ui.state.document.rules);
      ui.invalidateAllTestResults({ updateDom: false });
      ui.render();
      setNotice("JSON cargado en el borrador. Revísalo o pruébalo antes de Guardar.", ui.state.dirty ? "warn" : "ok");
    } catch (error) {
      setNotice(`No se pudo importar: ${error.message}`, "bad");
    } finally {
      input.value = "";
    }
  };

  ui.clearResolverCache = async function () {
    if (ui.state.cacheClearing) return;
    if (!window.confirm("Se eliminará únicamente la caché del Resolver TMDb. ¿Continuar?")) return;
    ui.state.cacheClearing = true;
    const button = document.getElementById("identity-clear-cache");
    if (button) { button.disabled = true; button.textContent = "Limpiando…"; }
    try {
      const payload = await ui.api(`${API_ROOT}/cache/clear`, { method: "POST", body: "{}" });
      if (!validCachePayload(payload)) {
        throw new Error("El motor confirmó HTTP 200 sin el contrato de caché esperado.");
      }
      ui.state.document.cache_status = payload.cache_status;
      if (ui.isActiveView()) ui.render();
      setNotice(`Caché del resolver limpiada: ${Number(payload.deleted || 0)} entradas eliminadas.`, "ok");
    } catch (error) {
      setNotice(`No se pudo limpiar la caché: ${error.message}`, "bad");
    } finally {
      ui.state.cacheClearing = false;
      const current = ui.isActiveView() ? document.getElementById("identity-clear-cache") : null;
      if (current) { current.disabled = false; current.textContent = "Limpiar caché del resolver"; }
    }
  };

  ui.show = async function () {
    activateMainTab();
    const section = activeSectionFromState();
    ui.state.section = section;
    rememberSection(section);
    if (!location.hash.startsWith("#limpieza-arr/")) {
      history.replaceState(null, "", `#limpieza-arr/${section}`);
    }
    if (!ui.state.document || !ui.state.draft) await ui.loadRules();
    else ui.render();
  };

  document.addEventListener("click", event => {
    const button = event.target.closest?.('[data-view="limpieza-arr"]');
    if (!button) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    const section = ui.state.section === "resolver" ? "resolver" : activeSectionFromState();
    const nextHash = `#limpieza-arr/${section}`;
    if (location.hash !== nextHash) location.hash = nextHash;
    else ui.show();
  }, true);

  window.addEventListener("beforeunload", event => {
    if (!ui.state.dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });
})();
