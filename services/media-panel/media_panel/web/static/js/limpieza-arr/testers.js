(function () {
  "use strict";

  const ui = window.ArrIdentityUI;

  ui.renderTester = function (section) {
    const parser = section === "parser";
    const category = ui.state.testCategories[section];
    return `<section class="identity-tester rule-group">
      <div class="identity-tester-heading">
        <div><h3>Probar título</h3><p>Usa los valores que ves ahora, incluso antes de guardarlos.</p></div>
        <span class="pill info">${parser ? "Solo parser" : "Consulta TMDb"}</span>
      </div>
      <div class="identity-test-form">
        <input id="identity-test-name" type="text" value="${ui.esc(ui.state.testNames[section])}" placeholder="Escribe aquí el nombre sucio completo" autocomplete="off" spellcheck="false">
        <select id="identity-test-category">
          ${parser ? `<option value="auto" ${category === "auto" ? "selected" : ""}>Automática</option>` : ""}
          <option value="movies" ${category === "movies" ? "selected" : ""}>Película</option>
          <option value="tv" ${category === "tv" ? "selected" : ""}>Serie</option>
        </select>
        <button type="button" class="btn primary" id="identity-test-button">Probar título</button>
      </div>
      <div id="identity-test-result">${ui.renderTestResult(section, ui.state.lastResult[section])}</div>
    </section>`;
  };

  ui.bindTester = function () {
    const name = document.getElementById("identity-test-name");
    const category = document.getElementById("identity-test-category");
    const button = document.getElementById("identity-test-button");
    if (!name || !category || !button) return;
    name.addEventListener("input", () => { ui.state.testNames[ui.state.section] = name.value; });
    category.addEventListener("change", () => { ui.state.testCategories[ui.state.section] = category.value; });
    button.addEventListener("click", ui.runTitleTest);
    ui.bindCandidateActions();
  };

  ui.bindCandidateActions = function () {
    document.querySelectorAll("[data-candidate-action]").forEach(candidateButton => {
      candidateButton.addEventListener("click", () => ui.addCandidateRule(candidateButton.dataset));
    });
  };

  ui.runTitleTest = async function () {
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
    button.disabled = true;
    button.textContent = "Probando…";
    resultBox.innerHTML = `<div class="identity-test-loading">Analizando el título…</div>`;
    try {
      const result = await ui.api(`/api/identity-rules/test-${section}`, {
        method: "POST",
        body: JSON.stringify({ name, category, rules: ui.state.draft })
      });
      ui.state.lastResult[section] = result;
      resultBox.innerHTML = ui.renderTestResult(section, result);
      ui.bindCandidateActions();
      ui.status("Prueba terminada. No se ha guardado ni movido ningún archivo.", "ok");
    } catch (error) {
      resultBox.innerHTML = `<div class="identity-test-error"><strong>Error de prueba</strong><span>${ui.esc(error.message)}</span></div>`;
      ui.status(`Error probando: ${error.message}`, "bad");
    } finally {
      button.disabled = false;
      button.textContent = "Probar título";
    }
  };

  ui.renderTestResult = function (section, payload) {
    if (!payload) return `<div class="identity-test-empty">Aquí aparecerá el resultado completo.</div>`;
    return section === "parser" ? ui.renderParserResult(payload) : ui.renderResolverResult(payload);
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
      ${steps ? `<div class="identity-table-wrap"><table class="table identity-trace"><thead><tr><th>Regla</th><th>Antes</th><th>Después</th></tr></thead><tbody>${steps}</tbody></table></div>` : ""}`;
  };

  ui.renderResolverResult = function (payload) {
    const identity = payload.identity || {};
    const details = payload.details || {};
    const candidates = payload.candidates?.length ? payload.candidates : (details.candidates || []);
    const score = Number(identity.score ?? details.top_score ?? candidates[0]?.score ?? 0);
    const margin = Number(identity.margin ?? details.margin ?? 0);
    const threshold = Number(ui.getPath(ui.state.draft, "resolver.acceptance.min_score") ?? 75);
    const minMargin = Number(ui.getPath(ui.state.draft, "resolver.acceptance.min_margin") ?? 12);
    const progress = threshold === 0 ? 100 : Math.round((score / threshold) * 1000) / 10;
    const tone = String(payload.status || "").startsWith("ACCEPT") ? "ok" : payload.ok === false ? "bad" : "warn";
    const rows = candidates.map(candidate => `<tr>
      <td>${ui.esc(candidate.tmdb_id ?? "-")}</td><td>${ui.esc(candidate.title || "-")}</td>
      <td>${ui.esc(candidate.year ?? "-")}</td><td><strong>${ui.esc(candidate.score ?? "-")}</strong></td>
      <td>${ui.esc((candidate.reasons || []).join(" · "))}</td>
      <td class="identity-candidate-actions">
        <button type="button" class="btn ghost small" data-candidate-action="alias" data-tmdb-id="${ui.esc(candidate.tmdb_id)}" data-title="${ui.esc(candidate.title)}" data-year="${ui.esc(candidate.year ?? "")}">Crear alias</button>
        <button type="button" class="btn ghost small" data-candidate-action="forced" data-tmdb-id="${ui.esc(candidate.tmdb_id)}" data-title="${ui.esc(candidate.title)}" data-year="${ui.esc(candidate.year ?? "")}">Forzar TMDb</button>
      </td>
    </tr>`).join("");
    const queries = (payload.queries || []).map(item => `<li><code>${ui.esc(item.params?.query || item.endpoint || "-")}</code><span>${ui.esc(item.params?.language || "")} ${ui.esc(item.params?.year || "")}</span><b>${ui.esc(item.status_code || "")}</b></li>`).join("");
    return `<div class="identity-decision ${tone}">
        <span class="pill ${tone}">${ui.esc(payload.status || "-")}</span>
        <strong>${score} puntos de ${threshold} · ${progress}% del umbral</strong>
        <span>Margen ${margin} de ${minMargin}</span>
      </div>
      ${payload.message ? `<div class="identity-result-message">${ui.esc(payload.message)}</div>` : ""}
      ${rows ? `<div class="identity-table-wrap"><table class="table"><thead><tr><th>TMDb</th><th>Título</th><th>Año</th><th>Puntos</th><th>Desglose</th><th>Acciones</th></tr></thead><tbody>${rows}</tbody></table></div>` : `<div class="identity-test-empty">No hay candidatos para mostrar.</div>`}
      ${queries ? `<details class="identity-queries"><summary>Consultas TMDb realizadas (${(payload.queries || []).length})</summary><ul>${queries}</ul></details>` : ""}`;
  };

  ui.addCandidateRule = function (dataset) {
    const category = ui.state.testCategories.resolver === "tv" ? "tv" : "movies";
    const raw = ui.state.testNames.resolver.trim();
    if (!raw || !dataset.title) return;
    const path = dataset.candidateAction === "alias"
      ? `resolver.aliases.${category}`
      : `resolver.forced_matches.${category}`;
    const list = ui.getPath(ui.state.draft, path) || [];
    const value = dataset.candidateAction === "alias"
      ? `${raw} | ${dataset.title}`
      : category === "movies"
        ? `${dataset.title} | ${dataset.year || ""} | ${dataset.tmdbId}`
        : `${dataset.title} | ${dataset.year || ""} | ${dataset.tmdbId}`;
    if (!list.includes(value)) list.push(value);
    ui.markDirty();
    ui.status(`${dataset.candidateAction === "alias" ? "Alias" : "Coincidencia forzada"} añadida al formulario. Falta guardar.`, "warn");
    ui.render();
  };

  ui.compactValue = function (value) {
    if (value === null || value === undefined) return "-";
    if (typeof value === "string") return value;
    try { return JSON.stringify(value); } catch (_error) { return String(value); }
  };
})();
