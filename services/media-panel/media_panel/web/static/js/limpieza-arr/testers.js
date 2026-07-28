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
        ${parser ? "" : `<label class="identity-sr-only" for="identity-test-source-title">Título mostrado en el buscador</label>
        <input class="identity-test-source-title" id="identity-test-source-title" type="text" maxlength="512" value="${ui.esc(ui.state.testSourceTitles[section])}" placeholder="Título mostrado en el buscador (opcional)" autocomplete="off" spellcheck="false">`}
      </div>
      <div id="identity-test-result">${testingHere ? `<div class="identity-test-loading">Analizando el título…</div>` : ui.renderTestResult(section, ui.state.lastResult[section], ui.state.testContext[section])}</div>
    </section>`;
  };

  ui.bindTester = function () {
    const name = document.getElementById("identity-test-name");
    const category = document.getElementById("identity-test-category");
    const sourceTitle = document.getElementById("identity-test-source-title");
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
    sourceTitle?.addEventListener("input", () => {
      ui.state.testSourceTitles[section] = sourceTitle.value;
      ui.invalidateTestResult(section);
    });
    sourceTitle?.addEventListener("keydown", event => {
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
    const sourceTitle = document.getElementById("identity-test-source-title")?.value.trim() || "";
    ui.state.testNames[section] = name;
    ui.state.testCategories[section] = category;
    ui.state.testSourceTitles[section] = sourceTitle;
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
      let result;
      try {
        result = await ui.api(`/api/identity-rules/test-${section}`, {
          method: "POST",
          body: JSON.stringify({ name, category, source_title: sourceTitle, rules: submittedRules })
        });
      } catch (error) {
        const errorPayload = error?.payload;
        if (section !== "resolver" || !errorPayload || typeof errorPayload !== "object" || Array.isArray(errorPayload)) throw error;
        result = errorPayload;
      }
      if (!ui.isCurrentTestRequest(section, requestId)) return;
      const parserResult = section === "parser"
        ? (result.result || result)
        : (result.parser_test?.result || result.parser_test || {});
      const context = Object.freeze({
        requestId,
        name,
        category: ["movies", "tv"].includes(String(parserResult.category || "").trim())
          ? String(parserResult.category).trim()
          : category,
        parserTitle: String(parserResult.title || "").trim(),
        parserYear: parserResult.year === null || parserResult.year === undefined
          ? ""
          : String(parserResult.year).trim(),
        sourceTitle
      });
      ui.state.lastResult[section] = result;
      ui.state.testContext[section] = context;
      if (ui.isActiveView() && ui.state.section === section) {
        const currentBox = document.getElementById("identity-test-result");
        if (currentBox) currentBox.innerHTML = ui.renderTestResult(section, result, context);
        ui.bindCandidateActions();
        const resolverAnnouncement = section === "resolver"
          ? ui.resolverPresentation(result, ui.resolverCandidates(result)).title
          : "";
        ui.status(
          `${resolverAnnouncement ? `${resolverAnnouncement}. ` : ""}${result.ok === false
            ? "Prueba terminada con una incidencia. No se ha guardado ni movido ningún archivo."
            : "Prueba terminada. No se ha guardado ni movido ningún archivo."}`,
          result.ok === false ? "warn" : "ok"
        );
      }
    } catch (error) {
      if (!ui.isCurrentTestRequest(section, requestId)) return;
      const resolverFailure = section === "resolver" ? {
        ok: false,
        status: "REQUEST_ERROR",
        message: String(error?.message || "No se pudo completar la petición."),
        decision: {
          status: "REQUEST_ERROR",
          accepted: false,
          has_scoring: false,
          bypass: false
        }
      } : null;
      const failureContext = resolverFailure ? Object.freeze({
        requestId,
        name,
        category,
        parserTitle: "",
        parserYear: "",
        sourceTitle
      }) : null;
      ui.state.lastResult[section] = resolverFailure;
      ui.state.testContext[section] = failureContext;
      if (ui.isActiveView() && ui.state.section === section) {
        const currentBox = document.getElementById("identity-test-result");
        if (currentBox) currentBox.innerHTML = resolverFailure
          ? ui.renderResolverResult(resolverFailure, failureContext)
          : `<div class="identity-test-error"><strong>Error de prueba</strong><span>${ui.esc(error.message)}</span></div>`;
        ui.status(
          resolverFailure
            ? "PRUEBA NO COMPLETADA. No se ha guardado ni movido ningún archivo."
            : `Error probando: ${error.message}`,
          "bad"
        );
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
