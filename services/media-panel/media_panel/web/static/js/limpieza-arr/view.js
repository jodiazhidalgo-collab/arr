(function () {
  "use strict";

  const ui = window.ArrIdentityUI;
  if (!ui) return;

  const API_ROOT = "/api/identity-rules";
  const PROFILE_STORAGE_KEY = "arr-identity-profile";
  const MAX_IMPORT_BYTES = 4 * 1024 * 1024;

  function sectionStorageKey(profile) {
    return `arr-identity-section-${profile}`;
  }

  function isObject(value) {
    return Boolean(value) && typeof value === "object" && !Array.isArray(value);
  }

  function sameValue(left, right) {
    try { return JSON.stringify(left) === JSON.stringify(right); } catch (_error) { return false; }
  }

  function validRulesDocument(payload, profile) {
    return isObject(payload)
      && payload.ok === true
      && (!payload.profile || payload.profile === profile)
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

  function storedProfile() {
    const profile = ui.storageGet(PROFILE_STORAGE_KEY, "common");
    return ui.profileConfig[profile] ? profile : "common";
  }

  function storedSection(profile) {
    const section = ui.storageGet(sectionStorageKey(profile), "parser");
    return ["parser", "resolver"].includes(section) ? section : "parser";
  }

  ui.resolveTarget = function (hash = location.hash) {
    const exact = ui.identityRouteFromHash(hash);
    if (exact) return exact;

    const legacy = String(hash || "").match(/^#limpieza-arr(?:\/(parser|resolver))?$/);
    if (legacy) {
      const profile = "common";
      const section = legacy[1] || storedSection(profile);
      return Object.freeze({ profile, section, hash: `#identidad/comun/${section}`, legacy: true });
    }

    const partial = String(hash || "").match(/^#identidad(?:\/(comun|peliculas|series))?(?:\/(parser|resolver))?$/);
    if (!partial) return null;
    const profile = ui.profileFromSlug(partial[1]) || storedProfile();
    const section = partial[2] || storedSection(profile);
    return Object.freeze({
      profile,
      section,
      hash: `#identidad/${ui.profileSlug(profile)}/${section}`,
      partial: true
    });
  };

  function rememberRoute(profile, section) {
    ui.storageSet(PROFILE_STORAGE_KEY, profile);
    ui.storageSet(sectionStorageKey(profile), section);
  }

  function activateMainTab() {
    document.querySelectorAll(".tabs [data-view]").forEach(button => {
      button.classList.toggle("active", button.dataset.view === "identidad");
    });
    const pageTitle = document.getElementById("title");
    if (pageTitle) pageTitle.textContent = "Identidad ARR";
  }

  function confirmDraftLoss(state, message) {
    return !state.dirty || window.confirm(message);
  }

  function cacheSummary(cache) {
    if (!cache || cache.available === false) return "No disponible";
    const active = cache.active ?? cache.valid ?? cache.entries ?? cache.total;
    const total = cache.total ?? cache.entries;
    if (active !== undefined && total !== undefined && active !== total) return `${active} activas de ${total}`;
    if (total !== undefined) return `${total} entradas`;
    return "Disponible";
  }

  function renderMetadata(state) {
    const documentState = state.document || {};
    const rules = state.draft || {};
    const fingerprint = String(documentState.fingerprint || "");
    return `<section class="identity-meta-card">
      <div class="identity-card-heading"><div><span class="identity-kicker">Configuración activa</span><h3>Metadatos</h3></div>
        <span class="pill ${state.dirty ? "warn" : "ok"}">${state.dirty ? "Borrador" : "Sin cambios"}</span>
      </div>
      <dl class="identity-meta-list">
        <div><dt>Perfil</dt><dd>${ui.esc(ui.profileLabel(state.profile))}</dd></div>
        <div><dt>Revisión</dt><dd>${ui.esc(documentState.revision ?? 0)}</dd></div>
        <div><dt>Guardada</dt><dd>${ui.esc(ui.formatDate(documentState.saved_at))}</dd></div>
        <div><dt>Esquema</dt><dd>v${ui.esc(rules.schema_version ?? "-")}</dd></div>
        <div><dt>Huella</dt><dd><code title="${ui.esc(fingerprint)}">${ui.esc(fingerprint ? `${fingerprint.slice(0, 22)}…` : "-")}</code></dd></div>
        <div><dt>Origen</dt><dd><code>${ui.esc(documentState.rules_path || "-")}</code></dd></div>
      </dl>
    </section>`;
  }

  function renderHistory(state) {
    const history = [...(state.document?.history || [])].reverse();
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

  function renderCacheCard(state) {
    if (state.section !== "resolver") return "";
    const cache = state.document?.cache_status || {};
    return `<section class="identity-meta-card identity-cache-card">
      <div class="identity-card-heading"><div><span class="identity-kicker">Resolver TMDb</span><h3>Caché</h3></div>
        <span class="pill ${cache.available === false ? "warn" : "info"}">${ui.esc(cacheSummary(cache))}</span></div>
      <p>Las reglas no cambian al vaciarla. Las próximas resoluciones volverán a consultar TMDb.</p>
    </section>`;
  }

  function renderToolbar(state) {
    const saving = Boolean(state.saving);
    return `<div class="identity-toolbar toolbar">
      <div id="identity-status" class="status identity-status ${ui.esc(state.notice?.tone || "info")}" role="status" aria-live="polite">
        ${ui.esc(state.notice?.message || "Configuración preparada.")}
      </div>
      <div class="toolbar-actions identity-toolbar-actions">
        <button type="button" class="btn ghost" id="identity-reload">Recargar</button>
        <button type="button" class="btn ghost" id="identity-reset">Restablecer</button>
        <button type="button" class="btn primary" id="identity-save" data-tooltip="Orquestador ${ui.esc(API_ROOT)}/${ui.esc(state.profile)}" ${!state.dirty || saving ? "disabled" : ""} ${!state.dirty && !saving ? 'data-idle-disabled="true"' : ""}>${saving ? "Guardando…" : "Guardar"}</button>
        <button type="button" class="btn ghost" id="identity-export">Exportar</button>
        <button type="button" class="btn ghost" id="identity-import">Importar</button>
        ${state.section === "resolver" ? `<button type="button" class="btn ghost danger" id="identity-clear-cache" ${state.cacheClearing ? "disabled" : ""}>${state.cacheClearing ? "Limpiando…" : "Limpiar caché"}</button>` : ""}
      </div>
    </div>`;
  }

  function renderLoading(profile) {
    if (!ui.isProfileActive(profile)) return;
    const app = document.getElementById("app");
    if (!app) return;
    app.innerHTML = `<section class="panel identity-loading"><span class="identity-spinner" aria-hidden="true"></span><div><h2>Identidad ARR · ${ui.esc(ui.profileLabel(profile))}</h2><p>Cargando reglas del motor…</p></div></section>`;
  }

  function renderLoadError(error, profile) {
    if (!ui.isProfileActive(profile)) return;
    const app = document.getElementById("app");
    if (!app) return;
    app.innerHTML = `<section class="panel identity-load-error">
      <span class="pill bad">No disponible</span><div><h2>No se pudieron cargar las reglas de ${ui.esc(ui.profileLabel(profile))}</h2><p>${ui.esc(error.message || error)}</p></div>
      <button type="button" class="btn primary" id="identity-load-retry">Reintentar</button>
    </section>`;
    document.getElementById("identity-load-retry")?.addEventListener("click", () => ui.loadRules({ replace: true, profile }));
  }

  ui.render = function () {
    const route = ui.identityRouteFromHash();
    if (!route || route.profile !== ui.activeProfile) return;
    const app = document.getElementById("app");
    const state = ui.state;
    if (!app || !state.document || !state.draft) return;
    const section = state.section === "resolver" ? "resolver" : "parser";
    const sectionSchema = state.document.schema?.[section] || { title: section, groups: [] };
    const dirtyText = state.dirty ? "Cambios sin guardar" : "Configuración guardada";

    app.innerHTML = `<section class="identity-shell" data-identity-section="${section}" data-identity-profile="${ui.esc(state.profile)}">
      <nav class="identity-profile-tabs segmented-tabs" aria-label="Perfil de Identidad ARR">
        ${Object.entries(ui.profileConfig).map(([profile, config]) => `<button type="button" class="${profile === state.profile ? "active" : ""}" data-identity-profile="${ui.esc(profile)}" aria-current="${profile === state.profile ? "page" : "false"}">${ui.esc(config.label)}</button>`).join("")}
      </nav>

      <header class="identity-hero">
        <div><span class="identity-kicker">Nombre sucio → identidad segura · ${ui.esc(ui.profileLabel(state.profile))}</span>
          <h2>${section === "parser" ? "Limpiador / Parser" : "Resolver TMDb"}</h2>
          <p>${section === "parser"
            ? "Limpia el release, extrae título, año, temporada y episodios antes de consultar servicios externos."
            : "Construye candidatos, puntúa las coincidencias y decide cuándo una identidad es suficientemente segura."}</p>
        </div>
        <div class="identity-hero-state">
          ${state.profile === "common" ? '<span class="pill warn">Afecta a ambos</span>' : ""}
          <span class="pill ${state.dirty ? "warn" : "ok"}">${dirtyText}</span>
          <small>${ui.esc(ui.activeMetadata(state.profile))}</small>
        </div>
      </header>

      <nav class="identity-subtabs" role="tablist" aria-label="Sección de Identidad ARR">
        <button id="identity-tab-parser" type="button" role="tab" aria-selected="${section === "parser"}" aria-controls="identity-panel-parser" tabindex="${section === "parser" ? "0" : "-1"}" class="${section === "parser" ? "active" : ""}" data-identity-section="parser">
          <span>1</span><div><strong>Parser</strong><small>Limpieza y lectura</small></div>
        </button>
        <button id="identity-tab-resolver" type="button" role="tab" aria-selected="${section === "resolver"}" aria-controls="identity-panel-resolver" tabindex="${section === "resolver" ? "0" : "-1"}" class="${section === "resolver" ? "active" : ""}" data-identity-section="resolver">
          <span>2</span><div><strong>Resolver TMDb</strong><small>Candidatos y puntuación</small></div>
        </button>
      </nav>

      <div id="identity-panel-${section}" class="identity-tabpanel" role="tabpanel" aria-labelledby="identity-tab-${section}" tabindex="0">
        ${renderToolbar(state)}
        <div class="identity-workspace">
          <main class="identity-editor">
            ${ui.renderTester(section)}
            <section class="identity-schema-heading">
              <div><span class="identity-kicker">Controles del motor</span><h3>${ui.esc(sectionSchema.title || "Reglas")}</h3></div>
              <span class="pill info">Esquema dinámico</span>
            </section>
            <div class="identity-groups">${ui.renderGroups(sectionSchema) || `<div class="empty">No hay controles para esta sección.</div>`}</div>
          </main>
          <aside class="identity-sidebar">${renderMetadata(state)}${renderCacheCard(state)}${renderHistory(state)}</aside>
        </div>
      </div>
      <div id="identity-panel-${section === "parser" ? "resolver" : "parser"}" class="identity-tabpanel" role="tabpanel" aria-labelledby="identity-tab-${section === "parser" ? "resolver" : "parser"}" hidden></div>
      <input id="identity-import-file" type="file" accept="application/json,.json" data-profile="${ui.esc(state.profile)}" hidden>
    </section>`;
    ui.bindView();
    ui.bindControls();
    ui.bindTester();
    if (state.focusTabAfterRender) {
      state.focusTabAfterRender = false;
      document.getElementById(`identity-tab-${section}`)?.focus();
    }
  };

  ui.bindView = function () {
    document.querySelectorAll("[data-identity-profile]").forEach(button => {
      if (!button.matches("button")) return;
      button.addEventListener("click", () => ui.switchProfile(button.dataset.identityProfile));
    });
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

  ui.switchProfile = function (profile) {
    if (!ui.profileConfig[profile]) return;
    ui.storeOpenGroups?.();
    const section = storedSection(profile);
    rememberRoute(profile, section);
    const nextHash = `#identidad/${ui.profileSlug(profile)}/${section}`;
    if (location.hash !== nextHash) location.hash = nextHash;
    else ui.show();
  };

  ui.switchSection = function (section, { focusTab = false } = {}) {
    if (!["parser", "resolver"].includes(section)) return;
    const state = ui.state;
    ui.storeOpenGroups?.();
    state.section = section;
    state.focusTabAfterRender = focusTab;
    rememberRoute(state.profile, section);
    const nextHash = `#identidad/${ui.profileSlug(state.profile)}/${section}`;
    if (location.hash !== nextHash) location.hash = nextHash;
    else ui.render();
  };

  ui.loadRules = async function ({ replace = false, profile = ui.activeProfile } = {}) {
    const state = ui.states[profile];
    if (!state) return;
    if (state.loading) {
      if (!state.document) renderLoading(profile);
      return;
    }
    const hadDocument = Boolean(state.document && state.draft);
    const requestEpoch = ++state.requestEpoch;
    state.loading = true;
    if (!hadDocument) renderLoading(profile);
    try {
      const payload = await ui.api(`${API_ROOT}/${profile}`);
      if (requestEpoch !== state.requestEpoch) return;
      if (!validRulesDocument(payload, profile)) {
        throw new Error("El motor no devolvió el contrato completo de reglas.");
      }
      state.document = payload;
      state.draft = ui.clone(payload.rules);
      state.dirty = false;
      ui.invalidateAllTestResults({ updateDom: false, profile });
      state.notice = payload.repair_required
        ? {
            message: "La configuración guardada no es válida. El motor usa valores seguros; pulsa Restablecer y después Guardar para repararla.",
            tone: "warn"
          }
        : { message: replace ? "Configuración recargada desde el motor." : "Configuración cargada.", tone: "ok" };
      if (ui.isProfileActive(profile)) ui.render();
    } catch (error) {
      if (requestEpoch !== state.requestEpoch) return;
      if (hadDocument) {
        if (ui.isProfileActive(profile)) ui.render();
        ui.status(`No se pudo recargar; el borrador se conserva: ${error.message}`, "bad", profile);
      } else {
        state.notice = { message: error.message, tone: "bad" };
        renderLoadError(error, profile);
      }
    } finally {
      if (requestEpoch === state.requestEpoch) state.loading = false;
    }
  };

  ui.reloadRules = function () {
    const state = ui.state;
    if (!confirmDraftLoss(state, "Recargar descartará el borrador sin guardar. ¿Continuar?")) return;
    return ui.loadRules({ replace: true, profile: state.profile });
  };

  ui.resetDraft = function () {
    const state = ui.state;
    if (!confirmDraftLoss(state, "Restablecer sustituirá el borrador actual por los valores de fábrica. ¿Continuar?")) return;
    state.draft = ui.clone(state.document.defaults);
    state.dirty = !sameValue(state.draft, state.document.rules)
      || Boolean(state.document.repair_required);
    ui.invalidateAllTestResults({ updateDom: false, profile: state.profile });
    ui.render();
    document.getElementById("identity-reset")?.focus();
    ui.status(state.dirty
      ? "Valores de fábrica cargados en el borrador. Pulsa Guardar para aplicarlos."
      : "La configuración activa ya coincide con los valores de fábrica.", state.dirty ? "warn" : "ok", state.profile);
  };

  ui.saveRules = async function () {
    const state = ui.state;
    const profile = state.profile;
    if (!state.dirty || state.saving) return;
    const submittedDraft = ui.clone(state.draft);
    const saveEpoch = ++state.saveEpoch;
    state.saving = true;
    ui.render();
    try {
      const payload = await ui.api(`${API_ROOT}/${profile}`, {
        method: "POST",
        body: JSON.stringify({
          rules: submittedDraft,
          expected_revision: Number(state.document.revision || 0)
        })
      });
      if (saveEpoch !== state.saveEpoch) return;
      if (!validRulesDocument(payload, profile)) {
        throw new Error("El motor confirmó HTTP 200 sin el contrato completo de reglas.");
      }
      const changedWhileSaving = !sameValue(state.draft, submittedDraft);
      const currentDraft = state.draft;
      state.document = payload;
      state.draft = changedWhileSaving ? currentDraft : ui.clone(payload.rules);
      state.dirty = changedWhileSaving && !sameValue(currentDraft, payload.rules);
      ui.invalidateAllTestResults({ updateDom: false, profile });
      if (ui.isProfileActive(profile)) ui.render();
      ui.status(changedWhileSaving
        ? "La revisión enviada se guardó; los cambios hechos durante el guardado siguen en el borrador."
        : payload.saved === false
          ? "No había cambios nuevos que guardar."
          : "Configuración guardada, versionada y activa para trabajos nuevos.", changedWhileSaving ? "warn" : "ok", profile);
    } catch (error) {
      if (saveEpoch !== state.saveEpoch) return;
      if (ui.isProfileActive(profile)) ui.render();
      const conflict = error.status === 409 || error.payload?.error === "revision_conflict";
      ui.status(conflict
        ? "Otra ventana guardó una revisión nueva. Tu borrador se conserva: expórtalo o recarga antes de reintentar."
        : `No se pudo guardar; el borrador se conserva: ${error.message}`, "bad", profile);
    } finally {
      if (saveEpoch !== state.saveEpoch) return;
      state.saving = false;
      if (ui.isProfileActive(profile)) {
        const save = document.getElementById("identity-save");
        if (save) {
          save.disabled = !state.dirty;
          save.toggleAttribute("data-idle-disabled", !state.dirty);
          save.textContent = "Guardar";
        }
      }
    }
  };

  ui.exportRules = function () {
    const state = ui.state;
    const payload = {
      exported_at: new Date().toISOString(),
      profile: state.profile,
      revision: state.document.revision ?? 0,
      fingerprint: state.document.fingerprint || null,
      rules: ui.clone(state.draft)
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    link.href = url;
    link.download = `arr-identidad-${state.profile}-rev-${payload.revision}-${stamp}.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    ui.status("Borrador exportado. No se ha guardado ningún cambio.", "ok", state.profile);
  };

  ui.openImport = function () {
    const state = ui.state;
    if (!confirmDraftLoss(state, "Importar sustituirá el borrador sin guardar. ¿Continuar?")) return;
    document.getElementById("identity-import-file")?.click();
  };

  ui.importRules = async function (event) {
    const input = event.currentTarget;
    const profile = input.dataset.profile;
    const state = ui.states[profile];
    const file = input.files?.[0];
    if (!state || !file) return;
    try {
      if (file.size > MAX_IMPORT_BYTES) throw new Error("El JSON supera el límite de 4 MB.");
      const parsed = JSON.parse(await file.text());
      const rules = isObject(parsed?.rules) ? parsed.rules : parsed;
      if (!isObject(rules) || !isObject(rules.parser) || !isObject(rules.resolver)) {
        throw new Error("El archivo no contiene reglas de Parser y Resolver.");
      }
      state.draft = ui.clone(rules);
      state.dirty = !sameValue(state.draft, state.document.rules);
      ui.invalidateAllTestResults({ updateDom: false, profile });
      if (ui.isProfileActive(profile)) ui.render();
      ui.status("JSON cargado en el borrador. Revísalo o pruébalo antes de Guardar.", state.dirty ? "warn" : "ok", profile);
    } catch (error) {
      ui.status(`No se pudo importar: ${error.message}`, "bad", profile);
    } finally {
      input.value = "";
    }
  };

  ui.clearResolverCache = async function () {
    const state = ui.state;
    const profile = state.profile;
    if (state.cacheClearing) return;
    if (!window.confirm("Se eliminará únicamente la caché del Resolver TMDb. ¿Continuar?")) return;
    state.cacheClearing = true;
    const button = document.getElementById("identity-clear-cache");
    if (button) { button.disabled = true; button.textContent = "Limpiando…"; }
    try {
      const payload = await ui.api(`${API_ROOT}/${profile}/cache/clear`, { method: "POST", body: "{}" });
      if (!validCachePayload(payload)) {
        throw new Error("El motor confirmó HTTP 200 sin el contrato de caché esperado.");
      }
      state.document.cache_status = payload.cache_status;
      if (ui.isProfileActive(profile)) ui.render();
      ui.status(`Caché del resolver limpiada: ${Number(payload.deleted || 0)} entradas eliminadas.`, "ok", profile);
    } catch (error) {
      ui.status(`No se pudo limpiar la caché: ${error.message}`, "bad", profile);
    } finally {
      state.cacheClearing = false;
      if (ui.isProfileActive(profile)) {
        const current = document.getElementById("identity-clear-cache");
        if (current) { current.disabled = false; current.textContent = "Limpiar caché"; }
      }
    }
  };

  ui.show = async function () {
    activateMainTab();
    const target = ui.resolveTarget(location.hash) || ui.resolveTarget("#identidad");
    if (location.hash !== target.hash) history.replaceState(null, "", target.hash);
    ui.setActiveProfile(target.profile);
    const state = ui.state;
    state.section = target.section;
    rememberRoute(target.profile, target.section);
    if (!state.document || !state.draft) await ui.loadRules({ profile: target.profile });
    else ui.render();
  };

  window.addEventListener("beforeunload", event => {
    if (!Object.values(ui.states).some(state => state.dirty)) return;
    event.preventDefault();
    event.returnValue = "";
  });
})();
