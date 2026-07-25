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

  ui.api = async function (path, options = {}) {
    const response = await fetch(path, {
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      ...options
    });
    const raw = await response.text();
    let payload = {};
    try { payload = raw ? JSON.parse(raw) : {}; } catch (_error) { payload = {}; }
    if (!response.ok) {
      const error = new Error(payload.message || payload.error || raw || `Error HTTP ${response.status}`);
      error.status = response.status;
      error.payload = payload;
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
    const box = document.getElementById("identity-status");
    if (!box) return;
    box.className = `status identity-status ${tone}`;
    box.textContent = message;
  };

  ui.markDirty = function () {
    ui.state.dirty = true;
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
