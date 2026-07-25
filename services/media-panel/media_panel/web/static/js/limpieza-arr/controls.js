(function () {
  "use strict";

  const ui = window.ArrIdentityUI;

  ui.renderGroups = function (sectionSchema) {
    return (sectionSchema?.groups || []).map((group, index) => {
      const open = ui.groupIsOpen(ui.state.section, group.id, index === 0);
      return `<details class="identity-group rule-group" data-group-id="${ui.esc(group.id)}" ${open ? "open" : ""}>
        <summary><span><strong>${ui.esc(group.title)}</strong><small>${ui.esc(group.description || "")}</small></span>
          <button type="button" class="btn ghost small" data-reset-group="${ui.esc(group.id)}">Restablecer bloque</button>
        </summary>
        <div class="identity-group-body">${(group.controls || []).map(ui.renderControl).join("")}</div>
      </details>`;
    }).join("");
  };

  ui.renderControl = function (control) {
    const value = ui.getPath(ui.state.draft, control.path);
    const input = ["tags", "mapping_rules"].includes(control.type)
      ? ui.renderList(control, Array.isArray(value) ? value : [])
      : control.type === "regex_pairs"
        ? ui.renderPairs(control, Array.isArray(value) ? value : [])
        : ui.renderScalar(control, value);
    return `<div class="identity-control" data-control-path="${ui.esc(control.path)}">
      <div class="identity-control-label">
        <label>${ui.esc(control.label)}</label>
        <small>${ui.esc(control.help || "")}</small>
      </div>
      <div class="identity-control-editor">${input}</div>
      <button type="button" class="btn ghost small identity-reset-control" data-reset-control="${ui.esc(control.path)}">Restablecer</button>
    </div>`;
  };

  ui.renderScalar = function (control, value) {
    const common = `data-identity-path="${ui.esc(control.path)}" data-identity-type="${ui.esc(control.type)}"`;
    if (control.type === "toggle") {
      return `<label class="toggle identity-toggle"><input ${common} type="checkbox" ${value ? "checked" : ""}> Activo</label>`;
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

  ui.renderList = function (control, values) {
    const rows = values.map((value, index) => `<div class="identity-list-row">
      <input type="text" value="${ui.esc(value)}" data-list-path="${ui.esc(control.path)}" data-list-index="${index}" spellcheck="false">
      <div class="identity-row-actions">
        <button type="button" class="btn ghost small" data-list-move="up" data-list-path="${ui.esc(control.path)}" data-list-index="${index}" title="Subir">↑</button>
        <button type="button" class="btn ghost small" data-list-move="down" data-list-path="${ui.esc(control.path)}" data-list-index="${index}" title="Bajar">↓</button>
        <button type="button" class="btn ghost small" data-list-duplicate data-list-path="${ui.esc(control.path)}" data-list-index="${index}" title="Duplicar">⧉</button>
        <button type="button" class="btn ghost small danger" data-list-delete data-list-path="${ui.esc(control.path)}" data-list-index="${index}" title="Eliminar">×</button>
      </div>
    </div>`).join("");
    return `<div class="identity-list" data-list="${ui.esc(control.path)}">${rows || `<div class="empty compact">Lista vacía.</div>`}</div>
      <button type="button" class="btn ghost small identity-add-row" data-list-add="${ui.esc(control.path)}">Añadir regla</button>`;
  };

  ui.renderPairs = function (control, values) {
    const rows = values.map((value, index) => `<div class="identity-pair-row">
      <input type="text" value="${ui.esc(value?.pattern || "")}" data-pair-path="${ui.esc(control.path)}" data-pair-index="${index}" data-pair-key="pattern" placeholder="Patrón" spellcheck="false">
      <input type="text" value="${ui.esc(value?.replacement || "")}" data-pair-path="${ui.esc(control.path)}" data-pair-index="${index}" data-pair-key="replacement" placeholder="Sustitución" spellcheck="false">
      <button type="button" class="btn ghost small danger" data-pair-delete data-pair-path="${ui.esc(control.path)}" data-pair-index="${index}">×</button>
    </div>`).join("");
    return `<div class="identity-pairs">${rows || `<div class="empty compact">Sin sustituciones.</div>`}</div>
      <button type="button" class="btn ghost small identity-add-row" data-pair-add="${ui.esc(control.path)}">Añadir sustitución</button>`;
  };

  ui.bindControls = function () {
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
      list.push("");
      ui.markDirty(); ui.render();
    }));
    document.querySelectorAll("[data-list-delete]").forEach(button => button.addEventListener("click", () => {
      const list = ui.getPath(ui.state.draft, button.dataset.listPath) || [];
      list.splice(Number(button.dataset.listIndex), 1);
      ui.markDirty(); ui.render();
    }));
    document.querySelectorAll("[data-list-duplicate]").forEach(button => button.addEventListener("click", () => {
      const list = ui.getPath(ui.state.draft, button.dataset.listPath) || [];
      const index = Number(button.dataset.listIndex);
      list.splice(index + 1, 0, list[index]);
      ui.markDirty(); ui.render();
    }));
    document.querySelectorAll("[data-list-move]").forEach(button => button.addEventListener("click", () => {
      const list = ui.getPath(ui.state.draft, button.dataset.listPath) || [];
      const index = Number(button.dataset.listIndex);
      const target = button.dataset.listMove === "up" ? index - 1 : index + 1;
      if (target < 0 || target >= list.length) return;
      [list[index], list[target]] = [list[target], list[index]];
      ui.markDirty(); ui.render();
    }));
    document.querySelectorAll("[data-pair-add]").forEach(button => button.addEventListener("click", () => {
      const list = ui.getPath(ui.state.draft, button.dataset.pairAdd) || [];
      list.push({ pattern: "", replacement: "" });
      ui.markDirty(); ui.render();
    }));
    document.querySelectorAll("[data-pair-delete]").forEach(button => button.addEventListener("click", () => {
      const list = ui.getPath(ui.state.draft, button.dataset.pairPath) || [];
      list.splice(Number(button.dataset.pairIndex), 1);
      ui.markDirty(); ui.render();
    }));
    document.querySelectorAll("[data-reset-control]").forEach(button => button.addEventListener("click", () => {
      ui.setPath(ui.state.draft, button.dataset.resetControl, ui.clone(ui.getPath(ui.state.document.defaults, button.dataset.resetControl)));
      ui.markDirty(); ui.render();
    }));
    document.querySelectorAll("[data-reset-group]").forEach(button => button.addEventListener("click", event => {
      event.preventDefault();
      const section = ui.state.document.schema[ui.state.section];
      const group = (section.groups || []).find(item => item.id === button.dataset.resetGroup);
      (group?.controls || []).forEach(control => ui.setPath(
        ui.state.draft,
        control.path,
        ui.clone(ui.getPath(ui.state.document.defaults, control.path))
      ));
      ui.markDirty(); ui.render();
    }));
    document.querySelectorAll(".identity-group").forEach(group => group.addEventListener("toggle", () => {
      ui.storeOpenGroups();
    }));
  };

  ui.groupIsOpen = function (section, id, fallback) {
    try {
      const stored = JSON.parse(localStorage.getItem(`arr-identity-open-${section}`) || "null");
      return Array.isArray(stored) ? stored.includes(id) : fallback;
    } catch (_error) { return fallback; }
  };

  ui.storeOpenGroups = function () {
    const open = [...document.querySelectorAll(".identity-group[open]")].map(group => group.dataset.groupId);
    localStorage.setItem(`arr-identity-open-${ui.state.section}`, JSON.stringify(open));
  };
})();
