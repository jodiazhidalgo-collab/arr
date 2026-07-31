(function () {
  "use strict";

  const ui = window.ArrIdentityUI = window.ArrIdentityUI || {};
  const PROFILE_CONFIG = Object.freeze({
    common: Object.freeze({ slug: "comun", label: "Común", defaultCategory: "auto" }),
    movies: Object.freeze({ slug: "peliculas", label: "Películas", defaultCategory: "movies" }),
    tv: Object.freeze({ slug: "series", label: "Series", defaultCategory: "tv" })
  });

  function createProfileState(profile) {
    const category = PROFILE_CONFIG[profile].defaultCategory;
    return {
      profile,
      document: null,
      draft: null,
      dirty: false,
      loading: false,
      saving: false,
      cacheClearing: false,
      requestEpoch: 0,
      saveEpoch: 0,
      section: "parser",
      lastResult: { parser: null, resolver: null },
      testContext: { parser: null, resolver: null },
      testRequestIds: { parser: 0, resolver: 0 },
      activeTest: null,
      testNames: { parser: "", resolver: "" },
      testCategories: { parser: category, resolver: category === "auto" ? "movies" : category },
      notice: null
    };
  }

  ui.profileConfig = PROFILE_CONFIG;
  ui.states = Object.fromEntries(Object.keys(PROFILE_CONFIG).map(profile => [
    profile,
    createProfileState(profile)
  ]));
  ui.activeProfile = "common";
  Object.defineProperty(ui, "state", {
    configurable: true,
    enumerable: true,
    get() { return ui.states[ui.activeProfile]; }
  });

  ui.clone = value => JSON.parse(JSON.stringify(value));
  ui.esc = value => String(value ?? "").replace(/[&<>"']/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
  }[ch]));

  ui.getPath = function (object, path) {
    return String(path || "").split(".").reduce(
      (value, key) => value && value[key] !== undefined ? value[key] : undefined,
      object
    );
  };

  ui.setPath = function (object, path, value) {
    const parts = String(path || "").split(".");
    let cursor = object;
    parts.slice(0, -1).forEach(key => {
      if (!cursor[key] || typeof cursor[key] !== "object" || Array.isArray(cursor[key])) cursor[key] = {};
      cursor = cursor[key];
    });
    cursor[parts.at(-1)] = value;
  };

  ui.profileFromSlug = function (slug) {
    return Object.keys(PROFILE_CONFIG).find(profile => PROFILE_CONFIG[profile].slug === slug) || null;
  };

  ui.profileSlug = function (profile = ui.activeProfile) {
    return PROFILE_CONFIG[profile]?.slug || PROFILE_CONFIG.common.slug;
  };

  ui.profileLabel = function (profile = ui.activeProfile) {
    return PROFILE_CONFIG[profile]?.label || PROFILE_CONFIG.common.label;
  };

  ui.setActiveProfile = function (profile) {
    if (PROFILE_CONFIG[profile]) ui.activeProfile = profile;
    return ui.state;
  };

  ui.identityApiRoot = function (profile = ui.activeProfile) {
    return `/api/identity-rules/${profile}`;
  };

  ui.identityRouteFromHash = function (hash = location.hash) {
    const match = String(hash || "").match(/^#identidad\/(comun|peliculas|series)\/(parser|resolver)$/);
    if (!match) return null;
    return Object.freeze({
      profile: ui.profileFromSlug(match[1]),
      section: match[2],
      hash: `#identidad/${match[1]}/${match[2]}`
    });
  };

  ui.isActiveView = function () {
    return Boolean(ui.identityRouteFromHash());
  };

  ui.isProfileActive = function (profile, section = null) {
    const route = ui.identityRouteFromHash();
    return Boolean(route)
      && route.profile === profile
      && (section === null || route.section === section);
  };

  ui.storageGet = function (key, fallback = null) {
    try {
      const value = localStorage.getItem(key);
      return value === null ? fallback : value;
    } catch (_error) {
      return fallback;
    }
  };

  ui.storageSet = function (key, value) {
    try {
      localStorage.setItem(key, value);
      return true;
    } catch (_error) {
      return false;
    }
  };

  ui.api = async function (path, options = {}) {
    const headers = { "Accept": "application/json", ...(options.headers || {}) };
    if (options.body !== undefined && !Object.keys(headers).some(key => key.toLowerCase() === "content-type")) {
      headers["Content-Type"] = "application/json";
    }
    const response = await fetch(path, {
      cache: "no-store",
      ...options,
      headers
    });
    const raw = await response.text();
    let payload;
    try {
      payload = raw ? JSON.parse(raw) : {};
    } catch (_error) {
      const error = new Error(`Respuesta JSON no válida (HTTP ${response.status}).`);
      error.status = response.status;
      throw error;
    }
    if (!response.ok) {
      const error = new Error(payload.message || payload.error || raw || `Error HTTP ${response.status}`);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      const error = new Error("El servidor no devolvió un objeto JSON.");
      error.status = response.status;
      throw error;
    }
    return payload;
  };

  ui.formatDate = function (value) {
    if (!value) return "Nunca";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("es-ES");
  };

  ui.sectionFromHash = function () {
    return ui.identityRouteFromHash()?.section || "parser";
  };

  ui.status = function (message, tone = "info", profile = ui.activeProfile) {
    const state = ui.states[profile];
    if (!state) return;
    state.notice = { message, tone };
    if (!ui.isProfileActive(profile)) return;
    const box = document.getElementById("identity-status");
    if (!box) return;
    box.className = `status identity-status ${tone}`;
    box.textContent = message;
  };

  ui.invalidateTestResult = function (section, { updateDom = true, profile = ui.activeProfile } = {}) {
    const state = ui.states[profile];
    if (!state || !Object.prototype.hasOwnProperty.call(state.lastResult, section)) return;
    state.testRequestIds[section] = Number(state.testRequestIds[section] || 0) + 1;
    state.lastResult[section] = null;
    state.testContext[section] = null;
    if (!updateDom || !ui.isProfileActive(profile, section)) return;
    const box = document.getElementById("identity-test-result");
    if (box && typeof ui.renderTestResult === "function") {
      box.innerHTML = ui.renderTestResult(section, null);
    }
    const button = document.getElementById("identity-test-button");
    if (button) {
      button.disabled = Boolean(state.activeTest);
      button.textContent = state.activeTest ? "Prueba en curso…" : "Probar título";
    }
  };

  ui.invalidateAllTestResults = function ({ updateDom = true, profile = ui.activeProfile } = {}) {
    ui.invalidateTestResult("parser", { updateDom, profile });
    ui.invalidateTestResult("resolver", { updateDom, profile });
  };

  ui.beginTestRequest = function (section, profile = ui.activeProfile) {
    const state = ui.states[profile];
    if (!state || state.activeTest) return null;
    ui.invalidateTestResult(section, { updateDom: false, profile });
    const request = Object.freeze({
      profile,
      section,
      requestId: state.testRequestIds[section],
      epoch: state.requestEpoch
    });
    state.activeTest = request;
    return request;
  };

  ui.isCurrentTestRequest = function (request) {
    const state = ui.states[request?.profile];
    return Boolean(state)
      && state.activeTest === request
      && Number(state.testRequestIds[request.section]) === Number(request.requestId);
  };

  ui.finishTestRequest = function (request) {
    const state = ui.states[request?.profile];
    if (!state || state.activeTest !== request) return false;
    state.activeTest = null;
    return true;
  };

  ui.markDirty = function () {
    const state = ui.state;
    state.dirty = true;
    ui.invalidateAllTestResults({ profile: state.profile });
    ui.status("Cambios sin guardar.", "warn", state.profile);
    const save = document.getElementById("identity-save");
    if (save) save.disabled = false;
  };

  ui.activeMetadata = function (profile = ui.activeProfile) {
    const documentState = ui.states[profile]?.document || {};
    const fingerprint = String(documentState.fingerprint || "");
    return `Revisión ${documentState.revision ?? 0} · ${ui.formatDate(documentState.saved_at)}`
      + (fingerprint ? ` · ${fingerprint.slice(0, 18)}…` : "");
  };
})();
