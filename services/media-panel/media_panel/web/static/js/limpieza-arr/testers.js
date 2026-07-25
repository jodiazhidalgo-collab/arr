(function () {
  "use strict";

  const ui = window.ArrIdentityUI;

  ui.renderTester = function (section) {
    const parser = section === "parser";
    const category = ui.state.testCategories[section];
    const activeTest = ui.state.activeTest;
    const testing = Boolean(activeTest);
    const testingHere = activeTest?.section === section;
    return `<section class="identity-tester rule-group" aria-labelledby="identity-tester-title">
      <div class="identity-tester-heading">
        <div><h3 id="identity-tester-title">Probar título</h3><p>Usa los valores que ves ahora, incluso antes de guardarlos.</p></div>
        <span class="pill info">${parser ? "Solo parser" : "Consulta TMDb"}</span>
      </div>
      <div class="identity-test-form">
        <label class="identity-sr-only" for="identity-test-name">Nombre a probar</label>
        <input id="identity-test-name" type="text" value="${ui.esc(ui.state.testNames[section])}" placeholder="Escribe aquí el nombre sucio completo" autocomplete="off" spellcheck="false">
        <label class="identity-sr-only" for="identity-test-category">Categoría</label>
        <select id="identity-test-category">
          ${parser ? `<option value="auto" ${category === "auto" ? "selected" : ""}>Automática</option>` : ""}
          <option value="movies" ${category === "movies" ? "selected" : ""}>Película</option>
          <option value="tv" ${category === "tv" ? "selected" : ""}>Serie</option>
        </select>
        <button type="button" class="btn primary" id="identity-test-button" ${testing ? "disabled" : ""}>${testingHere ? "Probando…" : testing ? "Prueba en curso…" : "Probar título"}</button>
      </div>
      <div id="identity-test-result" aria-live="polite">${testingHere ? `<div class="identity-test-loading">Analizando el título…</div>` : ui.renderTestResult(section, ui.state.lastResult[section], ui.state.testContext[section])}</div>
    </section>`;
  };

  ui.bindTester = function () {
    const name = document.getElementById("identity-test-name");
    const category = document.getElementById("identity-test-category");
    const button = document.getElementById("identity-test-button");
    if (!name || !category || !button) return;
    const section = ui.state.section;
    name.addEventListener("input", () => {
      ui.state.testNames[section] = name.value;
      ui.invalidateTestResult(section);
    });
    name.addEventListener("keydown", event => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      button.click();
    });
    category.addEventListener("change", () => {
      ui.state.testCategories[section] = category.value;
      ui.invalidateTestResult(section);
    });
    button.addEventListener("click", ui.runTitleTest);
    ui.bindCandidateActions();
  };

  ui.bindCandidateActions = function () {
    document.querySelectorAll("[data-candidate-action]").forEach(candidateButton => {
      candidateButton.addEventListener("click", () => ui.addCandidateRule(candidateButton.dataset));
    });
  };

  ui.runTitleTest = async function () {
    if (ui.state.activeTest) {
      ui.status("Ya hay una prueba de título en curso. Espera a que termine.", "info");
      const activeButton = document.getElementById("identity-test-button");
      if (activeButton) {
        activeButton.disabled = true;
        activeButton.textContent = "Prueba en curso…";
      }
      return;
    }
    const section = ui.state.section;
    const name = document.getElementById("identity-test-name")?.value.trim() || "";
    const category = document.getElementById("identity-test-category")?.value || (section === "parser" ? "auto" : "movies");
    ui.state.testNames[section] = name;
    ui.state.testCategories[section] = category;
    if (!name) {
      ui.status("Escribe un nombre antes de probar.", "warn");
      document.getElementById("identity-test-name")?.focus();
      return;
    }
    const button = document.getElementById("identity-test-button");
    const resultBox = document.getElementById("identity-test-result");
    if (!button || !resultBox) return;
    const request = ui.beginTestRequest(section);
    if (!request) return;
    const { requestId } = request;
    const submittedRules = ui.clone(ui.state.draft);
    button.disabled = true;
    button.textContent = "Probando…";
    resultBox.innerHTML = `<div class="identity-test-loading">Analizando el título…</div>`;
    try {
      const result = await ui.api(`/api/identity-rules/test-${section}`, {
        method: "POST",
        body: JSON.stringify({ name, category, rules: submittedRules })
      });
      if (!ui.isCurrentTestRequest(section, requestId)) return;
      const parserResult = section === "parser"
        ? (result.result || result)
        : (result.parser_test?.result || result.parser_test || {});
      const context = Object.freeze({
        requestId,
        name,
        category: String(parserResult.category || "").trim(),
        parserTitle: String(parserResult.title || "").trim(),
        parserYear: parserResult.year === null || parserResult.year === undefined
          ? ""
          : String(parserResult.year).trim()
      });
      ui.state.lastResult[section] = result;
      ui.state.testContext[section] = context;
      if (ui.isActiveView() && ui.state.section === section) {
        const currentBox = document.getElementById("identity-test-result");
        if (currentBox) currentBox.innerHTML = ui.renderTestResult(section, result, context);
        ui.bindCandidateActions();
        ui.status("Prueba terminada. No se ha guardado ni movido ningún archivo.", "ok");
      }
    } catch (error) {
      if (!ui.isCurrentTestRequest(section, requestId)) return;
      ui.state.lastResult[section] = null;
      ui.state.testContext[section] = null;
      if (ui.isActiveView() && ui.state.section === section) {
        const currentBox = document.getElementById("identity-test-result");
        if (currentBox) currentBox.innerHTML = `<div class="identity-test-error"><strong>Error de prueba</strong><span>${ui.esc(error.message)}</span></div>`;
        ui.status(`Error probando: ${error.message}`, "bad");
      }
    } finally {
      const released = ui.finishTestRequest(request);
      if (released && ui.isActiveView()) {
        const currentButton = document.getElementById("identity-test-button");
        if (currentButton) {
          currentButton.disabled = false;
          currentButton.textContent = "Probar título";
        }
      }
    }
  };

  ui.renderTestResult = function (section, payload, context = ui.state.testContext[section]) {
    if (!payload) return `<div class="identity-test-empty">Aquí aparecerá el resultado completo.</div>`;
    return section === "parser" ? ui.renderParserResult(payload) : ui.renderResolverResult(payload, context);
  };

  ui.renderParserResult = function (payload) {
    const result = payload.result || payload;
    const tv = result.tv || {};
    const cards = [
      ["Estado", payload.status || "-"], ["Título", result.title || "-"],
      ["Año", result.year ?? "-"], ["Categoría", result.category || "-"],
      ["Confianza", result.confidence || "-"], ["GuessIt", result.guessit || "-"],
      ["Temporada", tv.season ?? "-"], ["Episodios", (tv.episodes || []).join(", ") || "-"]
    ];
    const steps = (result.steps || []).map(step => `<tr>
      <td>${ui.esc(step.rule || step.name || "-")}</td>
      <td>${ui.esc(ui.compactValue(step.before))}</td>
      <td>${ui.esc(ui.compactValue(step.after))}</td>
    </tr>`).join("");
    return `<div class="identity-result-grid">${cards.map(([label, value]) =>
      `<div class="identity-result-card"><small>${ui.esc(label)}</small><strong>${ui.esc(value)}</strong></div>`
    ).join("")}</div>
      <div class="identity-result-wide"><small>Nombre limpio</small><strong>${ui.esc(result.cleaned || "-")}</strong></div>
      <div class="identity-result-wide"><small>Candidatos</small><strong>${ui.esc((result.candidates || []).join(" · ") || "-")}</strong></div>
      ${steps ? `<div class="identity-table-wrap"><table class="table identity-trace"><thead><tr><th scope="col">Regla</th><th scope="col">Antes</th><th scope="col">Después</th></tr></thead><tbody>${steps}</tbody></table></div>` : ""}`;
  };

  ui.renderResolverResult = function (payload, context) {
    const identity = payload.identity || {};
    const details = payload.details || {};
    const candidates = payload.candidates?.length ? payload.candidates : (details.candidates || []);
    const score = Number(identity.score ?? details.top_score ?? candidates[0]?.score ?? 0);
    const margin = Number(identity.margin ?? details.margin ?? 0);
    const threshold = Number(ui.getPath(ui.state.draft, "resolver.acceptance.min_score") ?? 75);
    const minMargin = Number(ui.getPath(ui.state.draft, "resolver.acceptance.min_margin") ?? 12);
    const progress = threshold === 0 ? 100 : Math.round((score / threshold) * 1000) / 10;
    const tone = String(payload.status || "").startsWith("ACCEPT") ? "ok" : payload.ok === false ? "bad" : "warn";
    const parserTitle = String(context?.parserTitle || "").trim();
    const parserYear = String(context?.parserYear || "").trim();
    const category = ["movies", "tv"].includes(context?.category) ? context.category : "";
    const requestId = Number(context?.requestId || 0);
    const rows = candidates.map(candidate => {
      const canAlias = Boolean(category && parserTitle && candidate.title);
      const canForce = Boolean(category && parserTitle && candidate.tmdb_id && (category === "tv" || parserYear));
      const forceHint = !category
        ? "El parser no determinó si es película o serie."
        : category === "movies" && !parserYear
        ? "El parser no encontró año; una coincidencia forzada de película no sería válida."
        : "Crear coincidencia forzada con el título y año extraídos por el parser.";
      return `<tr>
      <td>${ui.esc(candidate.tmdb_id ?? "-")}</td><td>${ui.esc(candidate.title || "-")}</td>
      <td>${ui.esc(candidate.year ?? "-")}</td><td><strong>${ui.esc(candidate.score ?? "-")}</strong></td>
      <td>${ui.esc((candidate.reasons || []).join(" · "))}</td>
      <td class="identity-candidate-actions">
        <button type="button" class="btn ghost small" data-candidate-action="alias" data-test-request-id="${requestId}" data-tmdb-id="${ui.esc(candidate.tmdb_id)}" data-candidate-title="${ui.esc(candidate.title)}" ${canAlias ? "" : "disabled"}>Crear alias</button>
        <button type="button" class="btn ghost small" data-candidate-action="forced" data-test-request-id="${requestId}" data-tmdb-id="${ui.esc(candidate.tmdb_id)}" title="${ui.esc(forceHint)}" ${canForce ? "" : "disabled"}>Forzar TMDb</button>
      </td>
    </tr>`;
    }).join("");
    const queries = (payload.queries || []).map(item => `<li><code>${ui.esc(item.params?.query || item.endpoint || "-")}</code><span>${ui.esc(item.params?.language || "")} ${ui.esc(item.params?.year || "")}</span><b>${ui.esc(item.status_code || "")}</b></li>`).join("");
    return `<div class="identity-decision ${tone}">
        <span class="pill ${tone}">${ui.esc(payload.status || "-")}</span>
        <strong>${score} puntos de ${threshold} · ${progress}% del umbral</strong>
        <span>Margen ${margin} de ${minMargin}</span>
      </div>
      ${payload.message ? `<div class="identity-result-message">${ui.esc(payload.message)}</div>` : ""}
      ${rows ? `<div class="identity-table-wrap"><table class="table"><thead><tr><th scope="col">TMDb</th><th scope="col">Título</th><th scope="col">Año</th><th scope="col">Puntos</th><th scope="col">Desglose</th><th scope="col">Acciones</th></tr></thead><tbody>${rows}</tbody></table></div>` : `<div class="identity-test-empty">No hay candidatos para mostrar.</div>`}
      ${queries ? `<details class="identity-queries"><summary>Consultas TMDb realizadas (${(payload.queries || []).length})</summary><ul>${queries}</ul></details>` : ""}`;
  };

  ui.addCandidateRule = function (dataset) {
    const context = ui.state.testContext.resolver;
    if (!context || Number(dataset.testRequestId) !== Number(context.requestId)) {
      ui.status("Esta prueba ya no es válida. Vuelve a pulsar Probar título.", "warn");
      return;
    }
    const category = ["movies", "tv"].includes(context.category) ? context.category : "";
    const parserTitle = String(context.parserTitle || "").trim();
    const parserYear = String(context.parserYear || "").trim();
    const candidateTitle = String(dataset.candidateTitle || "").trim();
    const tmdbId = String(dataset.tmdbId || "").trim();
    if (!category) {
      ui.status("El parser no determinó si es película o serie; no se puede crear la regla.", "warn");
      return;
    }
    if (!parserTitle || !tmdbId || (dataset.candidateAction === "alias" && !candidateTitle)) {
      ui.status("La prueba no contiene datos suficientes para crear esta regla.", "warn");
      return;
    }
    if (dataset.candidateAction === "forced" && category === "movies" && !parserYear) {
      ui.status("No se puede forzar una película sin año extraído por el parser.", "warn");
      return;
    }
    const path = dataset.candidateAction === "alias"
      ? `resolver.aliases.${category}`
      : `resolver.forced_matches.${category}`;
    const list = ui.getPath(ui.state.draft, path) || [];
    const value = dataset.candidateAction === "alias"
      ? `${parserTitle} | ${candidateTitle}`
      : category === "tv" && !parserYear
        ? `${parserTitle} | ${tmdbId}`
        : `${parserTitle} | ${parserYear} | ${tmdbId}`;
    const existingIndex = list.indexOf(value);
    if (existingIndex >= 0) {
      ui.status("Esa regla ya existe en el borrador.", "info");
      document.querySelector(`[data-list-path="${path}"][data-list-index="${existingIndex}"]`)?.focus();
      return;
    }
    const newIndex = list.length;
    list.push(value);
    ui.markDirty();
    ui.render();
    ui.status(`${dataset.candidateAction === "alias" ? "Alias" : "Coincidencia forzada"} añadida al formulario. Falta guardar.`, "warn");
    document.querySelector(`[data-list-path="${path}"][data-list-index="${newIndex}"]`)?.focus();
  };

  ui.compactValue = function (value) {
    if (value === null || value === undefined) return "-";
    if (typeof value === "string") return value;
    try { return JSON.stringify(value); } catch (_error) { return String(value); }
  };
})();
