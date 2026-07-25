(function () {
  "use strict";

  const ui = window.ArrIdentityUI = window.ArrIdentityUI || {};
  ui.state = {
    document: null,
    draft: null,
    dirty: false,
    loading: false,
    section: "parser",
    lastResult: { parser: null, resolver: null },
    testContext: { parser: null, resolver: null },
    testRequestIds: { parser: 0, resolver: 0 },
    activeTest: null,
    testNames: { parser: "", resolver: "" },
    testCategories: { parser: "auto", resolver: "movies" }
  };

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

  ui.isActiveView = function () {
    return location.hash === "#limpieza-arr" || location.hash.startsWith("#limpieza-arr/");
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
    const section = location.hash.replace(/^#limpieza-arr\/?/, "").split("/")[0];
    return section === "resolver" ? "resolver" : "parser";
  };

  ui.status = function (message, tone = "info") {
    ui.state.notice = { message, tone };
    if (!ui.isActiveView()) return;
    const box = document.getElementById("identity-status");
    if (!box) return;
    box.className = `status identity-status ${tone}`;
    box.textContent = message;
  };

  ui.invalidateTestResult = function (section, { updateDom = true } = {}) {
    if (!Object.prototype.hasOwnProperty.call(ui.state.lastResult, section)) return;
    ui.state.testRequestIds[section] = Number(ui.state.testRequestIds[section] || 0) + 1;
    ui.state.lastResult[section] = null;
    ui.state.testContext[section] = null;
    if (!updateDom || !ui.isActiveView() || ui.state.section !== section) return;
    const box = document.getElementById("identity-test-result");
    if (box && typeof ui.renderTestResult === "function") {
      box.innerHTML = ui.renderTestResult(section, null);
    }
    const button = document.getElementById("identity-test-button");
    if (button) {
      button.disabled = Boolean(ui.state.activeTest);
      button.textContent = ui.state.activeTest ? "Prueba en curso…" : "Probar título";
    }
  };

  ui.invalidateAllTestResults = function ({ updateDom = true } = {}) {
    ui.invalidateTestResult("parser", { updateDom });
    ui.invalidateTestResult("resolver", { updateDom });
  };

  ui.beginTestRequest = function (section) {
    if (ui.state.activeTest) return null;
    ui.invalidateTestResult(section, { updateDom: false });
    const request = Object.freeze({
      section,
      requestId: ui.state.testRequestIds[section],
    });
    ui.state.activeTest = request;
    return request;
  };

  ui.isCurrentTestRequest = function (section, requestId) {
    return Number(ui.state.testRequestIds[section]) === Number(requestId);
  };

  ui.finishTestRequest = function (request) {
    if (ui.state.activeTest !== request) return false;
    ui.state.activeTest = null;
    return true;
  };

  ui.markDirty = function () {
    ui.state.dirty = true;
    ui.invalidateAllTestResults();
    ui.status("Cambios sin guardar.", "warn");
    const save = document.getElementById("identity-save");
    if (save) save.disabled = false;
  };

  ui.activeMetadata = function () {
    const documentState = ui.state.document || {};
    const fingerprint = String(documentState.fingerprint || "");
    return `Revisión ${documentState.revision ?? 0} · ${ui.formatDate(documentState.saved_at)}`
      + (fingerprint ? ` · ${fingerprint.slice(0, 18)}…` : "");
  };
})();
