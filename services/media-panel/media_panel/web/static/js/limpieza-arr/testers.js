(function () {
  "use strict";

  const ui = window.ArrIdentityUI;

  ui.renderTester = function (section) {
    const state = ui.state;
    const parser = section === "parser";
    const category = state.testCategories[section];
    const activeTest = state.activeTest;
    const testing = Boolean(activeTest);
    const locked = Boolean(state.resetting);
    const testingHere = activeTest?.section === section;
    const categoryOptions = state.profile === "common"
      ? `${parser ? `<option value="auto" ${category === "auto" ? "selected" : ""}>Automática</option>` : ""}
          <option value="movies" ${category === "movies" ? "selected" : ""}>Película</option>
          <option value="tv" ${category === "tv" ? "selected" : ""}>Serie</option>`
      : state.profile === "movies"
        ? `<option value="movies" selected>Película</option>`
        : `<option value="tv" selected>Serie</option>`;
    return `<section class="identity-tester rule-group" aria-labelledby="identity-tester-title">
      <div class="identity-tester-heading">
        <div><h3 id="identity-tester-title">Probar título</h3><p>Usa los valores que ves ahora, incluso antes de guardarlos.</p></div>
        <span class="pill info">${parser ? "Solo parser" : "Consulta TMDb"}</span>
      </div>
      <div class="identity-test-form">
        <label class="identity-sr-only" for="identity-test-name">Nombre a probar</label>
        <input id="identity-test-name" type="text" value="${ui.esc(state.testNames[section])}" placeholder="Escribe aquí el nombre sucio completo" autocomplete="off" spellcheck="false" ${locked ? "disabled" : ""}>
        <label class="identity-sr-only" for="identity-test-category">Categoría</label>
        <select id="identity-test-category" ${locked ? "disabled" : ""}>${categoryOptions}</select>
        <button type="button" class="btn primary" id="identity-test-button" ${testing || locked ? "disabled" : ""}>${locked ? "Restablecimiento en curso…" : testingHere ? "Probando…" : testing ? "Prueba en curso…" : "Probar título"}</button>
      </div>
      <div id="identity-test-result">${testingHere ? `<div class="identity-test-loading">Analizando el título…</div>` : ui.renderTestResult(section, state.lastResult[section], state.testContext[section])}</div>
    </section>`;
  };

  ui.bindTester = function () {
    const name = document.getElementById("identity-test-name");
    const category = document.getElementById("identity-test-category");
    const button = document.getElementById("identity-test-button");
    if (!name || !category || !button) return;
    const state = ui.state;
    const profile = state.profile;
    if (state.resetting) return;
    const section = state.section;
    name.addEventListener("input", () => {
      state.testNames[section] = name.value;
      ui.invalidateTestResult(section, { profile });
    });
    name.addEventListener("keydown", event => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      button.click();
    });
    category.addEventListener("change", () => {
      state.testCategories[section] = category.value;
      ui.invalidateTestResult(section, { profile });
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
    const state = ui.state;
    const profile = state.profile;
    if (state.resetting) {
      ui.status("Espera a que termine el restablecimiento antes de probar.", "info", profile);
      return;
    }
    if (state.activeTest) {
      ui.status("Ya hay una prueba de título en curso. Espera a que termine.", "info", profile);
      const activeButton = document.getElementById("identity-test-button");
      if (activeButton) {
        activeButton.disabled = true;
        activeButton.textContent = "Prueba en curso…";
      }
      return;
    }
    const section = state.section;
    const name = document.getElementById("identity-test-name")?.value.trim() || "";
    const category = document.getElementById("identity-test-category")?.value || state.testCategories[section];
    state.testNames[section] = name;
    state.testCategories[section] = category;
    if (!name) {
      ui.status("Escribe un nombre antes de probar.", "warn", profile);
      document.getElementById("identity-test-name")?.focus();
      return;
    }
    const button = document.getElementById("identity-test-button");
    const resultBox = document.getElementById("identity-test-result");
    if (!button || !resultBox) return;
    const request = ui.beginTestRequest(section, profile);
    if (!request) return;
    const { requestId } = request;
    const submittedRules = ui.clone(state.draft);
    button.disabled = true;
    button.textContent = "Probando…";
    resultBox.innerHTML = `<div class="identity-test-loading">Analizando el título…</div>`;
    try {
      let result;
      try {
        result = await ui.api(`${ui.identityApiRoot(profile)}/test-${section}`, {
          method: "POST",
          body: JSON.stringify({ name, category, rules: submittedRules })
        });
      } catch (error) {
        const errorPayload = error?.payload;
        if (section !== "resolver" || !errorPayload || typeof errorPayload !== "object" || Array.isArray(errorPayload)) throw error;
        result = errorPayload;
      }
      if (!ui.isCurrentTestRequest(request)) return;
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
          : String(parserResult.year).trim()
      });
      state.lastResult[section] = result;
      state.testContext[section] = context;
      if (ui.isProfileActive(profile, section)) {
        const currentBox = document.getElementById("identity-test-result");
        if (currentBox) currentBox.innerHTML = ui.renderTestResult(section, result, context);
        ui.bindCandidateActions();
        const resolverPresentation = section === "resolver" ? ui.resolverPresentation(result) : null;
        const resolverAnnouncement = resolverPresentation?.title || "";
        const resolverAccepted = resolverPresentation?.tone === "ok";
        ui.status(
          `${resolverAnnouncement ? `${resolverAnnouncement}. ` : ""}${section === "resolver" && !resolverAccepted
            ? "Prueba terminada sin aceptar una identidad. No se ha guardado ni movido ningún archivo."
            : result.ok === false
              ? "Prueba terminada con una incidencia. No se ha guardado ni movido ningún archivo."
              : "Prueba terminada. No se ha guardado ni movido ningún archivo."}`,
          section === "resolver" ? resolverPresentation.tone : result.ok === false ? "warn" : "ok",
          profile
        );
      }
    } catch (error) {
      if (!ui.isCurrentTestRequest(request)) return;
      const resolverFailure = section === "resolver" ? {
        ok: false,
        status: "REQUEST_ERROR",
        message: String(error?.message || "No se pudo completar la petición."),
        decision: {
          status: "REQUEST_ERROR",
          accepted: false
        }
      } : null;
      const failureContext = resolverFailure ? Object.freeze({
        requestId,
        name,
        category,
        parserTitle: "",
        parserYear: ""
      }) : null;
      state.lastResult[section] = resolverFailure;
      state.testContext[section] = failureContext;
      if (ui.isProfileActive(profile, section)) {
        const currentBox = document.getElementById("identity-test-result");
        if (currentBox) currentBox.innerHTML = resolverFailure
          ? ui.renderResolverResult(resolverFailure, failureContext)
          : `<div class="identity-test-error"><strong>Error de prueba</strong><span>${ui.esc(error.message)}</span></div>`;
        ui.status(
          resolverFailure
            ? "PRUEBA NO COMPLETADA. No se ha guardado ni movido ningún archivo."
            : `Error probando: ${error.message}`,
          "bad",
          profile
        );
      }
    } finally {
      const released = ui.finishTestRequest(request);
      if (released && ui.isProfileActive(profile)) {
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

  ui.addCandidateRule = async function (dataset) {
    const state = ui.state;
    const sourceProfile = state.profile;
    if (state.readOnly || state.resetting) {
      ui.status(state.resetting ? "Espera a que termine el restablecimiento." : "Este documento histórico es de solo lectura.", "warn");
      return;
    }
    const context = state.testContext.resolver;
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
    let commonState = ui.states.common;
    if (!commonState?.draft || !commonState.document) {
      await ui.loadRules({ profile: "common" });
      commonState = ui.states.common;
    }
    if (!commonState?.draft || !commonState.document) {
      ui.status("No se pudo cargar la configuración Común; no se ha añadido ninguna regla.", "bad", sourceProfile);
      return;
    }
    if (state.testContext.resolver !== context || Number(dataset.testRequestId) !== Number(context.requestId)) {
      ui.status("Esta prueba ya no es válida. Vuelve a pulsar Probar título.", "warn", sourceProfile);
      return;
    }
    if (commonState.readOnly || commonState.resetting) {
      ui.status("La configuración Común no se puede editar ahora mismo.", "warn", sourceProfile);
      return;
    }
    let list = ui.getPath(commonState.draft, path);
    if (!Array.isArray(list)) {
      list = [];
      ui.setPath(commonState.draft, path, list);
    }
    const value = dataset.candidateAction === "alias"
      ? `${parserTitle} | ${candidateTitle}`
      : category === "tv" && !parserYear
        ? `${parserTitle} | ${tmdbId}`
        : `${parserTitle} | ${parserYear} | ${tmdbId}`;
    const existingIndex = list.indexOf(value);
    if (existingIndex >= 0) {
      ui.status("Esa regla ya existe en el borrador Común.", "info", sourceProfile);
      if (ui.isProfileActive("common")) {
        document.querySelector(`[data-list-path="${path}"][data-list-index="${existingIndex}"]`)?.focus();
      }
      return;
    }
    const newIndex = list.length;
    list.push(value);
    ui.markDirty("common");
    const ruleLabel = dataset.candidateAction === "alias" ? "Alias" : "Coincidencia forzada";
    ui.status(`${ruleLabel} añadida al borrador Común. Falta guardar.`, "warn", "common");
    if (sourceProfile !== "common") {
      ui.status(`${ruleLabel} añadida en Común. Entra en Común y pulsa Guardar.`, "warn", sourceProfile);
    }
    if (ui.isProfileActive("common")) {
      ui.render();
      document.querySelector(`[data-list-path="${path}"][data-list-index="${newIndex}"]`)?.focus();
    }
  };

  ui.compactValue = function (value) {
    if (value === null || value === undefined) return "-";
    if (typeof value === "string") return value;
    try { return JSON.stringify(value); } catch (_error) { return String(value); }
  };
})();
