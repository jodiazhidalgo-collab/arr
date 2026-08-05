(function () {
  "use strict";

  const ui = window.ArrIdentityUI;
  const LEGACY_RESOLVER_GROUPS = Object.freeze({
    common: Object.freeze({
      resolver_locales: "resolver_algorithm",
      resolver_evidence: "resolver_evidence",
      resolver_series_candidates: "resolver_queries",
      resolver_title_matching: "resolver_queries",
      resolver_search: "resolver_coverage",
      resolver_scoring: "resolver_adjudication",
      resolver_acceptance: "resolver_adjudication",
      resolver_operations: "resolver_operations"
    }),
    movies: Object.freeze({
      resolver_locales: "resolver_movies",
      resolver_evidence: "resolver_movies",
      resolver_series_candidates: "resolver_movies",
      resolver_title_matching: "resolver_movies",
      resolver_search: "resolver_movies",
      resolver_scoring: "resolver_movies",
      resolver_acceptance: "resolver_movies",
      resolver_operations: "resolver_movies"
    }),
    tv: Object.freeze({
      resolver_locales: "resolver_tv",
      resolver_evidence: "resolver_tv",
      resolver_series_candidates: "resolver_tv",
      resolver_title_matching: "resolver_tv",
      resolver_search: "resolver_tv",
      resolver_scoring: "resolver_tv",
      resolver_acceptance: "resolver_tv",
      resolver_operations: "resolver_tv"
    })
  });

  ui.controlId = function (path) {
    return `identity-control-${String(path || "field").replace(/[^a-z0-9_-]+/gi, "-")}`;
  };

  ui.controlsLocked = function () {
    return Boolean(ui.state.readOnly || ui.state.resetting);
  };

  ui.controlIsReadOnly = function (control) {
    return Boolean(control?.readonly === true || control?.type === "ordered_tags");
  };

  ui.renderGroups = function (sectionSchema) {
    return (sectionSchema?.groups || []).map((group, index) => {
      const open = ui.groupIsOpen(ui.state.section, group.id, index === 0);
      return `<details class="identity-group rule-group" data-group-id="${ui.esc(group.id)}" ${open ? "open" : ""}>
        <summary><span><strong>${ui.esc(group.title)}</strong><small>${ui.esc(group.description || "")}</small></span>
          <button type="button" class="btn ghost small" data-reset-group="${ui.esc(group.id)}" aria-label="Restablecer bloque ${ui.esc(group.title)}" ${ui.controlsLocked() ? "disabled" : ""}>Restablecer bloque</button>
        </summary>
        <div class="identity-group-body">${(group.controls || []).map(ui.renderControl).join("")}</div>
      </details>`;
    }).join("");
  };

  ui.renderControl = function (control) {
    const value = ui.getPath(ui.state.draft, control.path);
    const controlId = ui.controlId(control.path);
    const helpId = `${controlId}-help`;
    const values = Array.isArray(value) ? value : [];
    const readOnly = ui.controlIsReadOnly(control);
    const primaryId = readOnly
      ? `${controlId}-value`
      : ["tags", "mapping_rules", "regex_pairs"].includes(control.type)
      ? (values.length ? `${controlId}-0` : `${controlId}-add`)
      : controlId;
    const input = readOnly
      ? ui.renderReadOnlyControl(control, value, primaryId, helpId)
      : ["tags", "mapping_rules"].includes(control.type)
      ? ui.renderList(control, values, controlId, helpId)
      : control.type === "regex_pairs"
        ? ui.renderPairs(control, values, controlId, helpId)
        : ui.renderScalar(control, value, controlId, helpId);
    return `<div class="identity-control" data-control-path="${ui.esc(control.path)}">
      <div class="identity-control-label">
        ${readOnly
          ? `<span id="${ui.esc(`${controlId}-label`)}">${ui.esc(control.label)}</span>`
          : `<label for="${ui.esc(primaryId)}">${ui.esc(control.label)}</label>`}
        <small id="${ui.esc(helpId)}">${ui.esc(control.help || "")}</small>
      </div>
      <div class="identity-control-editor">${input}</div>
      ${readOnly ? "" : `<button type="button" class="btn ghost small identity-reset-control" data-reset-control="${ui.esc(control.path)}" aria-label="Restablecer ${ui.esc(control.label)}" ${ui.controlsLocked() ? "disabled" : ""}>Restablecer</button>`}
    </div>`;
  };

  ui.renderReadOnlyControl = function (control, value, valueId, helpId) {
    const labelledBy = `${ui.controlId(control.path)}-label`;
    if (Array.isArray(value)) {
      const items = value.map(item => `<li>${ui.esc(item)}</li>`).join("");
      return `<ol id="${ui.esc(valueId)}" class="identity-readonly-list" aria-labelledby="${ui.esc(labelledBy)}" aria-describedby="${ui.esc(helpId)}">${items || "<li>Sin valores.</li>"}</ol>`;
    }
    const text = value === null || value === undefined || value === "" ? "Sin valor." : String(value);
    return `<output id="${ui.esc(valueId)}" class="identity-readonly-value" aria-labelledby="${ui.esc(labelledBy)}" aria-describedby="${ui.esc(helpId)}">${ui.esc(text)}</output>`;
  };

  ui.renderScalar = function (control, value, controlId, helpId) {
    const common = `id="${ui.esc(controlId)}" aria-describedby="${ui.esc(helpId)}" data-identity-path="${ui.esc(control.path)}" data-identity-type="${ui.esc(control.type)}" ${ui.controlsLocked() ? "disabled" : ""}`;
    if (control.type === "toggle") {
      return `<label class="toggle identity-toggle" for="${ui.esc(controlId)}"><input ${common} type="checkbox" ${value ? "checked" : ""}> Activo</label>`;
    }
    if (control.type === "select") {
      return `<select ${common}>${(control.options || []).map(option =>
        `<option value="${ui.esc(option.value)}" ${option.value === value ? "selected" : ""}>${ui.esc(option.label)}</option>`
      ).join("")}</select>`;
    }
    if (["number", "decimal"].includes(control.type)) {
      return `<input ${common} type="number" value="${ui.esc(value ?? "")}" min="${ui.esc(control.min ?? "")}" max="${ui.esc(control.max ?? "")}" step="${ui.esc(control.step ?? (control.type === "decimal" ? 0.01 : 1))}">`;
    }
    return `<input ${common} type="text" value="${ui.esc(value ?? "")}" spellcheck="false">`;
  };

  ui.renderList = function (control, values, controlId, helpId) {
    const disabled = ui.controlsLocked() ? "disabled" : "";
    const rows = values.map((value, index) => `<div class="identity-list-row">
      <input id="${ui.esc(`${controlId}-${index}`)}" type="text" value="${ui.esc(value)}" data-list-path="${ui.esc(control.path)}" data-list-index="${index}" aria-label="${ui.esc(control.label)} ${index + 1}" aria-describedby="${ui.esc(helpId)}" spellcheck="false" ${disabled}>
      <div class="identity-row-actions">
        <button type="button" class="btn ghost small" data-list-move="up" data-list-path="${ui.esc(control.path)}" data-list-index="${index}" title="Subir" aria-label="Subir ${ui.esc(control.label)} ${index + 1}" ${disabled}>↑</button>
        <button type="button" class="btn ghost small" data-list-move="down" data-list-path="${ui.esc(control.path)}" data-list-index="${index}" title="Bajar" aria-label="Bajar ${ui.esc(control.label)} ${index + 1}" ${disabled}>↓</button>
        <button type="button" class="btn ghost small" data-list-duplicate data-list-path="${ui.esc(control.path)}" data-list-index="${index}" title="Duplicar" aria-label="Duplicar ${ui.esc(control.label)} ${index + 1}" ${disabled}>⧉</button>
        <button type="button" class="btn ghost small danger" data-list-delete data-list-path="${ui.esc(control.path)}" data-list-index="${index}" title="Eliminar" aria-label="Eliminar ${ui.esc(control.label)} ${index + 1}" ${disabled}>×</button>
      </div>
    </div>`).join("");
    return `<div class="identity-list" data-list="${ui.esc(control.path)}">${rows || `<div class="empty compact">Lista vacía.</div>`}</div>
      <button id="${ui.esc(`${controlId}-add`)}" type="button" class="btn ghost small identity-add-row" data-list-add="${ui.esc(control.path)}" aria-describedby="${ui.esc(helpId)}" ${disabled}>Añadir regla</button>`;
  };

  ui.renderPairs = function (control, values, controlId, helpId) {
    const disabled = ui.controlsLocked() ? "disabled" : "";
    const rows = values.map((value, index) => `<div class="identity-pair-row">
      <input id="${ui.esc(`${controlId}-${index}`)}" type="text" value="${ui.esc(value?.pattern || "")}" data-pair-path="${ui.esc(control.path)}" data-pair-index="${index}" data-pair-key="pattern" placeholder="Patrón" aria-label="Patrón de ${ui.esc(control.label)} ${index + 1}" aria-describedby="${ui.esc(helpId)}" spellcheck="false" ${disabled}>
      <input id="${ui.esc(`${controlId}-${index}-replacement`)}" type="text" value="${ui.esc(value?.replacement || "")}" data-pair-path="${ui.esc(control.path)}" data-pair-index="${index}" data-pair-key="replacement" placeholder="Sustitución" aria-label="Sustitución de ${ui.esc(control.label)} ${index + 1}" aria-describedby="${ui.esc(helpId)}" spellcheck="false" ${disabled}>
      <button type="button" class="btn ghost small danger" data-pair-delete data-pair-path="${ui.esc(control.path)}" data-pair-index="${index}" aria-label="Eliminar ${ui.esc(control.label)} ${index + 1}" ${disabled}>×</button>
    </div>`).join("");
    return `<div class="identity-pairs">${rows || `<div class="empty compact">Sin sustituciones.</div>`}</div>
      <button id="${ui.esc(`${controlId}-add`)}" type="button" class="btn ghost small identity-add-row" data-pair-add="${ui.esc(control.path)}" aria-describedby="${ui.esc(helpId)}" ${disabled}>Añadir sustitución</button>`;
  };

  ui.focusListPosition = function (path, index) {
    const values = ui.getPath(ui.state.draft, path) || [];
    const target = values.length
      ? document.getElementById(`${ui.controlId(path)}-${Math.max(0, Math.min(index, values.length - 1))}`)
      : document.getElementById(`${ui.controlId(path)}-add`);
    target?.focus();
  };

  ui.focusControl = function (path) {
    const wrapper = [...document.querySelectorAll("[data-control-path]")]
      .find(control => control.dataset.controlPath === path);
    wrapper?.querySelector("input, select, textarea, button[data-list-add], button[data-pair-add]")?.focus();
  };

  ui.focusGroupReset = function (groupId) {
    const button = [...document.querySelectorAll("[data-reset-group]")]
      .find(item => item.dataset.resetGroup === groupId);
    button?.focus();
  };

  ui.bindControls = function () {
    if (ui.controlsLocked()) {
      document.querySelectorAll(".identity-group").forEach(group => group.addEventListener("toggle", () => {
        ui.storeOpenGroups();
      }));
      return;
    }
    document.querySelectorAll("[data-identity-path]").forEach(input => {
      const update = () => {
        let value = input.type === "checkbox" ? input.checked : input.value;
        if (["number", "decimal"].includes(input.dataset.identityType)) value = Number(value);
        ui.setPath(ui.state.draft, input.dataset.identityPath, value);
        ui.markDirty();
      };
      input.addEventListener("input", update);
      input.addEventListener("change", update);
    });
    document.querySelectorAll("[data-list-path]").forEach(input => {
      if (!input.matches("input")) return;
      input.addEventListener("input", () => {
        const list = ui.getPath(ui.state.draft, input.dataset.listPath) || [];
        list[Number(input.dataset.listIndex)] = input.value;
        ui.markDirty();
      });
    });
    document.querySelectorAll("[data-pair-path]").forEach(input => {
      if (!input.matches("input")) return;
      input.addEventListener("input", () => {
        const list = ui.getPath(ui.state.draft, input.dataset.pairPath) || [];
        const index = Number(input.dataset.pairIndex);
        list[index] = list[index] || { pattern: "", replacement: "" };
        list[index][input.dataset.pairKey] = input.value;
        ui.markDirty();
      });
    });
    document.querySelectorAll("[data-list-add]").forEach(button => button.addEventListener("click", () => {
      const list = ui.getPath(ui.state.draft, button.dataset.listAdd) || [];
      const index = list.length;
      list.push("");
      ui.markDirty(); ui.render();
      document.querySelector(`[data-list-path="${button.dataset.listAdd}"][data-list-index="${index}"]`)?.focus();
    }));
    document.querySelectorAll("[data-list-delete]").forEach(button => button.addEventListener("click", () => {
      const list = ui.getPath(ui.state.draft, button.dataset.listPath) || [];
      const index = Number(button.dataset.listIndex);
      list.splice(index, 1);
      ui.markDirty(); ui.render();
      ui.focusListPosition(button.dataset.listPath, index);
    }));
    document.querySelectorAll("[data-list-duplicate]").forEach(button => button.addEventListener("click", () => {
      const list = ui.getPath(ui.state.draft, button.dataset.listPath) || [];
      const index = Number(button.dataset.listIndex);
      list.splice(index + 1, 0, list[index]);
      ui.markDirty(); ui.render();
      ui.focusListPosition(button.dataset.listPath, index + 1);
    }));
    document.querySelectorAll("[data-list-move]").forEach(button => button.addEventListener("click", () => {
      const list = ui.getPath(ui.state.draft, button.dataset.listPath) || [];
      const index = Number(button.dataset.listIndex);
      const target = button.dataset.listMove === "up" ? index - 1 : index + 1;
      if (target < 0 || target >= list.length) return;
      [list[index], list[target]] = [list[target], list[index]];
      ui.markDirty(); ui.render();
      ui.focusListPosition(button.dataset.listPath, target);
    }));
    document.querySelectorAll("[data-pair-add]").forEach(button => button.addEventListener("click", () => {
      const list = ui.getPath(ui.state.draft, button.dataset.pairAdd) || [];
      const index = list.length;
      list.push({ pattern: "", replacement: "" });
      ui.markDirty(); ui.render();
      document.querySelector(`[data-pair-path="${button.dataset.pairAdd}"][data-pair-index="${index}"][data-pair-key="pattern"]`)?.focus();
    }));
    document.querySelectorAll("[data-pair-delete]").forEach(button => button.addEventListener("click", () => {
      const list = ui.getPath(ui.state.draft, button.dataset.pairPath) || [];
      const index = Number(button.dataset.pairIndex);
      list.splice(index, 1);
      ui.markDirty(); ui.render();
      ui.focusListPosition(button.dataset.pairPath, index);
    }));
    document.querySelectorAll("[data-reset-control]").forEach(button => button.addEventListener("click", () => {
      const path = button.dataset.resetControl;
      ui.setPath(ui.state.draft, path, ui.clone(ui.getPath(ui.state.document.defaults, path)));
      ui.markDirty(); ui.render();
      ui.focusControl(path);
    }));
    document.querySelectorAll("[data-reset-group]").forEach(button => button.addEventListener("click", event => {
      event.preventDefault();
      const section = ui.state.document.schema[ui.state.section];
      const group = (section.groups || []).find(item => item.id === button.dataset.resetGroup);
      (group?.controls || []).filter(control => !ui.controlIsReadOnly(control)).forEach(control => ui.setPath(
        ui.state.draft,
        control.path,
        ui.clone(ui.getPath(ui.state.document.defaults, control.path))
      ));
      ui.markDirty(); ui.render();
      ui.focusGroupReset(button.dataset.resetGroup);
    }));
    document.querySelectorAll(".identity-group").forEach(group => group.addEventListener("toggle", () => {
      ui.storeOpenGroups();
    }));
  };

  ui.groupIsOpen = function (section, id, fallback) {
    try {
      const storageKey = `arr-identity-open-${ui.activeProfile}-${section}`;
      const stored = JSON.parse(ui.storageGet(storageKey, "null"));
      if (!Array.isArray(stored)) return fallback;
      const mapping = section === "resolver" ? LEGACY_RESOLVER_GROUPS[ui.activeProfile] : null;
      const migrated = [...new Set(stored.map(groupId => mapping?.[groupId] || groupId))];
      if (migrated.length !== stored.length || migrated.some((groupId, index) => groupId !== stored[index])) {
        ui.storageSet(storageKey, JSON.stringify(migrated));
      }
      return migrated.includes(id);
    } catch (_error) { return fallback; }
  };

  ui.storeOpenGroups = function () {
    const open = [...document.querySelectorAll(".identity-group[open]")].map(group => group.dataset.groupId);
    ui.storageSet(`arr-identity-open-${ui.activeProfile}-${ui.state.section}`, JSON.stringify(open));
  };
})();
