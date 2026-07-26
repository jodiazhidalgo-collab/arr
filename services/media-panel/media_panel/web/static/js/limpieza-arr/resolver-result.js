(function () {
  "use strict";

  const ui = window.ArrIdentityUI;

  ui.resolverNumber = function (value, maximumFractionDigits = 2) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return "-";
    return new Intl.NumberFormat("es-ES", {
      minimumFractionDigits: 0,
      maximumFractionDigits
    }).format(numeric);
  };

  ui.resolverSignedNumber = function (value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return "-";
    const formatted = ui.resolverNumber(numeric);
    return numeric > 0 ? `+${formatted}` : formatted;
  };

  ui.resolverLanguageName = function (value) {
    const code = String(value || "").trim().split("-", 1)[0].toLowerCase();
    if (!code) return "configurado";
    try {
      return new Intl.DisplayNames(["es"], { type: "language" }).of(code) || code.toUpperCase();
    } catch (_error) {
      return code.toUpperCase();
    }
  };

  ui.resolverControlLabels = function () {
    const labels = new Map();
    const groups = ui.state.document?.schema?.resolver?.groups || [];
    groups.forEach(group => (group.controls || []).forEach(control => {
      if (control?.path && control?.label) labels.set(String(control.path), String(control.label));
    }));
    return labels;
  };

  ui.resolverControlLabel = function (path, fallback) {
    return ui.resolverControlLabels().get(path) || fallback;
  };

  ui.resolverCandidates = function (payload) {
    if (Array.isArray(payload?.candidates) && payload.candidates.length) return payload.candidates;
    return Array.isArray(payload?.details?.candidates) ? payload.details.candidates : [];
  };

  ui.resolverStatus = function (payload) {
    return String(payload?.decision?.status || payload?.status || payload?.error || "UNKNOWN").toUpperCase();
  };

  ui.resolverPresentation = function (payload, candidates) {
    const status = ui.resolverStatus(payload);
    const decision = payload?.decision || {};
    const margin = Number(decision.margin);
    const scorePassed = decision.score_passed !== false;
    const marginPassed = decision.margin_passed !== false;
    const hasSecondCandidate = decision.has_second_candidate === undefined
      ? candidates.length > 1
      : Boolean(decision.has_second_candidate);
    const tie = decision.has_scoring && margin === 0 && candidates.length > 1;
    const bothFailed = decision.has_scoring && !scorePassed && !marginPassed;
    const languagePreference = decision?.original_language_preference || {};
    const languagePreferenceApplied = languagePreference.applied === true;
    const preferredLanguage = ui.resolverLanguageName(languagePreference.language);
    const oldestPreference = decision?.oldest_exact_title_preference || {};
    const oldestPreferenceApplied = oldestPreference.applied === true;
    const selectedOldestYear = Number(oldestPreference.selected_year);
    const sourceLabels = {
      tmdb_id: "un identificador TMDb directo",
      imdb_id: "un identificador IMDb directo",
      forced_match: "una coincidencia forzada validada"
    };

    if (status === "ACCEPTED") {
      const bypassSource = sourceLabels[decision.source];
      return {
        tone: "ok",
        title: "ACEPTADA",
        text: languagePreferenceApplied
          ? `El candidato se ha seleccionado porque es el único con idioma original ${preferredLanguage} dentro del grupo ambiguo.`
          : oldestPreferenceApplied
            ? `La película se ha seleccionado porque ${Number.isFinite(selectedOldestYear) ? `su año ${selectedOldestYear} es` : "es"} el más antiguo entre los candidatos con título y puntuación exactamente iguales.`
          : decision.bypass && bypassSource
          ? `Identidad aceptada mediante ${bypassSource}. Los umbrales no intervienen en esta decisión.`
          : hasSecondCandidate
            ? "El primer candidato cumple la puntuación y la ventaja mínimas configuradas."
            : "El único candidato cumple la puntuación mínima y el margen calculado al no existir un segundo candidato."
      };
    }
    if (status === "REJECTED_SCORE") {
      return {
        tone: "bad",
        title: bothFailed ? "RECHAZADA POR PUNTUACIÓN Y MARGEN" : "RECHAZADA POR PUNTUACIÓN",
        text: bothFailed
          ? "El primer candidato no alcanza la puntuación mínima ni la ventaja mínima configuradas."
          : "El primer candidato no alcanza la puntuación mínima configurada."
      };
    }
    if (status === "REJECTED_MARGIN") {
      return {
        tone: "warn",
        title: tie ? "RECHAZADA POR EMPATE" : "RECHAZADA POR MARGEN",
        text: tie
          ? "Los dos primeros candidatos tienen la misma puntuación y el motor no puede elegir uno con seguridad."
          : hasSecondCandidate
            ? "El primer candidato no obtiene la ventaja mínima configurada sobre el segundo."
            : "No existe un segundo candidato y la ventaja calculada contra cero no alcanza el margen mínimo."
      };
    }
    if (status === "NO_CANDIDATES") return { tone: "warn", title: "SIN CANDIDATOS", text: "TMDb no devolvió candidatos utilizables para las búsquedas realizadas." };
    if (status === "INVALID_RULES") return { tone: "bad", title: "CONFIGURACIÓN NO VÁLIDA", text: "El borrador contiene un valor que el motor no puede utilizar. Revisa el aviso de configuración." };
    if (status === "PARSER_ERROR") return { tone: "bad", title: "ERROR DEL PARSER", text: "Las reglas actuales no permiten analizar este nombre." };
    if (status === "TMDB_UNAVAILABLE") return { tone: "bad", title: "TMDB NO DISPONIBLE", text: "No se pudo completar la comunicación con TMDb." };
    if (status === "TMDB_ERROR") return { tone: "bad", title: "CONSULTA TMDB RECHAZADA", text: "TMDb rechazó una consulta o devolvió una respuesta no válida." };
    if (status === "ORCHESTRATOR_UNAVAILABLE") return { tone: "bad", title: "MOTOR NO DISPONIBLE", text: "El panel no pudo comunicarse con el motor ARR." };
    if (status === "INVALID_UPSTREAM_RESPONSE") return { tone: "bad", title: "RESPUESTA DEL MOTOR NO VÁLIDA", text: "El motor respondió, pero el panel no pudo interpretar el resultado." };
    if (status === "REQUEST_ERROR") return { tone: "bad", title: "PRUEBA NO COMPLETADA", text: "El navegador no pudo completar la petición al panel ARR." };
    if (status === "REJECTED") {
      const reason = String(payload?.details?.reason_code || "");
      if (reason === "category_conflict") return { tone: "warn", title: "CATEGORÍA CONTRADICTORIA", text: "El nombre analizado contradice la categoría seleccionada." };
      if (reason === "empty_title") return { tone: "warn", title: "TÍTULO NO IDENTIFICADO", text: "No se pudo obtener un título útil para consultar TMDb." };
      if (reason === "forced_target_invalid") return { tone: "warn", title: "TMDB FORZADO NO VÁLIDO", text: "El identificador fijado en la regla no existe o TMDb no permite validarlo." };
      if (reason === "forced_type_mismatch") return { tone: "warn", title: "TIPO FORZADO INCORRECTO", text: "El tipo o identificador devuelto por TMDb no coincide con la regla." };
      if (reason === "forced_title_mismatch") return { tone: "warn", title: "TÍTULO FORZADO INCORRECTO", text: "El título de la regla no coincide con los títulos reales de TMDb." };
      if (reason === "forced_year_mismatch") return { tone: "warn", title: "AÑO FORZADO INCORRECTO", text: "El año de la regla no coincide con el año real de TMDb." };
      if (reason.startsWith("forced_")) return { tone: "warn", title: "COINCIDENCIA FORZADA NO VÁLIDA", text: "La coincidencia forzada no concuerda con los datos reales de TMDb." };
      if (reason === "category_not_resolvable") return { tone: "warn", title: "CATEGORÍA NO RESOLUBLE", text: "La categoría seleccionada no permite consultar TMDb." };
      return { tone: "warn", title: "IDENTIDAD NO SEGURA", text: "El motor no dispone de evidencia suficiente para aceptar una identidad." };
    }
    return { tone: "bad", title: "PRUEBA NO COMPLETADA", text: "No se pudo obtener un resultado utilizable del motor ARR." };
  };

  ui.resolverConcreteCause = function (payload) {
    const status = ui.resolverStatus(payload);
    const details = payload?.details || {};
    if (status === "REJECTED" && details.reason_code === "forced_year_mismatch") {
      const expected = details.expected_year ?? "-";
      const returned = details.returned_year ?? "sin año";
      return `La regla exige ${expected} y TMDb devuelve ${returned}.`;
    }
    if (status === "REJECTED" && details.reason_code === "forced_type_mismatch") {
      const expected = details.media_type === "tv" ? "serie" : details.media_type === "movie" ? "película" : details.media_type;
      const returned = details.returned_media_type === "tv" ? "serie" : details.returned_media_type === "movie" ? "película" : details.returned_media_type;
      return expected && returned ? `La regla exige ${expected} y TMDb devuelve ${returned}.` : "";
    }
    const statusesWithUsefulMessage = new Set([
      "INVALID_RULES", "PARSER_ERROR", "TMDB_UNAVAILABLE", "TMDB_ERROR",
      "ORCHESTRATOR_UNAVAILABLE", "INVALID_UPSTREAM_RESPONSE", "REQUEST_ERROR"
    ]);
    if (!statusesWithUsefulMessage.has(status)) return "";
    const message = String(payload?.message || "").trim();
    if (message === "name debe contener un titulo valido.") return "El nombre debe contener un título válido.";
    if (message === "category debe ser movies, tv o auto.") return "La categoría debe ser Película o Serie.";
    return message;
  };

  ui.renderResolverDecision = function (payload, candidates) {
    const decision = payload?.decision || {};
    const presentation = ui.resolverPresentation(payload, candidates);
    const concreteCause = ui.resolverConcreteCause(payload);
    const scoreLabel = ui.resolverControlLabel("resolver.acceptance.min_score", "Puntuación mínima");
    const marginLabel = ui.resolverControlLabel("resolver.acceptance.min_margin", "Margen mínimo");
    const hasScoring = Boolean(decision.has_scoring);
    const bypass = Boolean(decision.bypass);
    const languagePreferenceApplied = decision?.original_language_preference?.applied === true;
    const oldestPreferenceApplied = decision?.oldest_exact_title_preference?.applied === true;
    const hasSecondCandidate = decision.has_second_candidate === undefined
      ? candidates.length > 1
      : Boolean(decision.has_second_candidate);
    const metrics = !hasScoring ? "" : `<div class="resolver-criteria" aria-label="Criterios de aceptación">
      <div class="resolver-criterion">
        <span>Puntuación obtenida</span><strong>${ui.esc(ui.resolverNumber(decision.score))}</strong>
        <span>${ui.esc(scoreLabel)}</span><strong>${ui.esc(ui.resolverNumber(decision.min_score))}</strong>
        <b class="${bypass ? "neutral" : decision.score_passed ? "pass" : "fail"}">${bypass ? "NO APLICA" : decision.score_passed ? "CUMPLIDA" : "NO CUMPLIDA"}</b>
      </div>
      <div class="resolver-criterion">
        ${hasSecondCandidate
          ? `<span>Ventaja sobre el segundo</span><strong>${ui.esc(ui.resolverNumber(decision.margin))}</strong>`
          : `<span>Segundo candidato</span><strong>No existe</strong><span>Ventaja calculada</span><strong>${ui.esc(ui.resolverNumber(decision.margin))}</strong>`}
        <span>${ui.esc(marginLabel)}</span><strong>${ui.esc(ui.resolverNumber(decision.min_margin))}</strong>
        <b class="${bypass ? "neutral" : languagePreferenceApplied || oldestPreferenceApplied || decision.margin_passed ? "pass" : "fail"}">${bypass ? "NO APLICA" : languagePreferenceApplied ? "RESUELTO POR IDIOMA" : oldestPreferenceApplied ? "RESUELTO POR MÁS ANTIGUA" : decision.margin_passed ? "CUMPLIDO" : "NO CUMPLIDO"}</b>
      </div>
    </div>`;
    return `<section class="resolver-outcome ${ui.esc(presentation.tone)}" aria-labelledby="resolver-outcome-title">
      <small>RESULTADO</small>
      <h4 id="resolver-outcome-title">${ui.esc(presentation.title)}</h4>
      <p>${ui.esc(presentation.text)}</p>
      ${concreteCause ? `<p class="resolver-concrete-cause"><strong>Motivo concreto:</strong> ${ui.esc(concreteCause)}</p>` : ""}
      ${metrics}
    </section>`;
  };

  ui.renderResolverBreakdown = function (candidate) {
    const items = Array.isArray(candidate?.breakdown) ? candidate.breakdown : [];
    const labels = ui.resolverControlLabels();
    const rows = items.map(item => {
      const path = String(item?.path || `resolver.scoring.${item?.key || ""}`);
      const label = labels.get(path) || "Concepto de puntuación";
      const applied = Number(item?.applied);
      return `<tr class="${applied < 0 ? "penalty" : "bonus"}">
        <th scope="row">${ui.esc(label)}</th>
        <td>${ui.esc(ui.resolverNumber(item?.configured))}</td>
        <td>${ui.esc(ui.resolverSignedNumber(applied))}</td>
      </tr>`;
    }).join("");
    if (!rows) return `<p class="resolver-no-breakdown">Este candidato no recibió puntos ni penalizaciones.</p>`;
    return `<div class="resolver-breakdown-wrap"><table class="resolver-breakdown">
      <thead><tr><th scope="col">Concepto</th><th scope="col">Configurado</th><th scope="col">Aplicado</th></tr></thead>
      <tbody>${rows}</tbody>
      <tfoot><tr><th scope="row">TOTAL</th><td></td><td>${ui.esc(ui.resolverNumber(candidate.score))}</td></tr></tfoot>
    </table></div>`;
  };

  ui.renderResolverCandidate = function (candidate, index, context) {
    const category = ["movies", "tv"].includes(context?.category) ? context.category : "";
    const parserTitle = String(context?.parserTitle || "").trim();
    const parserYear = String(context?.parserYear || "").trim();
    const candidateTitle = String(candidate?.title || "").trim();
    const tmdbId = String(candidate?.tmdb_id || "").trim();
    const requestId = Number(context?.requestId || 0);
    const canAlias = Boolean(category && parserTitle && candidateTitle);
    const canForce = Boolean(category && parserTitle && tmdbId && (category === "tv" || parserYear));
    const forceHint = !category
      ? "El parser no determinó si es película o serie."
      : category === "movies" && !parserYear
        ? "El parser no encontró año; no se puede fijar una película sin año."
        : "";
    const original = candidate?.original_title && candidate.original_title !== candidate.title
      ? `<span>Original: ${ui.esc(candidate.original_title)}</span>`
      : "";
    return `<details class="resolver-candidate ${index === 0 ? "top" : ""}">
      <summary>
        <span class="resolver-rank">${index + 1}.º</span>
        <span class="resolver-candidate-name"><strong>${ui.esc(candidate?.title || "Sin título")}</strong><small>TMDb ${ui.esc(candidate?.tmdb_id ?? "-")} ${original}</small></span>
        <span class="resolver-year">${ui.esc(candidate?.year ?? "Sin año")}</span>
        <strong class="resolver-score">${ui.esc(ui.resolverNumber(candidate?.score))} puntos</strong>
        <span class="resolver-open-label">Ver puntuación</span>
      </summary>
      <div class="resolver-candidate-body">
        ${ui.renderResolverBreakdown(candidate)}
        <div class="resolver-candidate-actions">
          <button type="button" class="btn ghost small" data-candidate-action="alias" data-test-request-id="${requestId}" data-tmdb-id="${ui.esc(tmdbId)}" data-candidate-title="${ui.esc(candidateTitle)}" ${canAlias ? "" : "disabled"}>Crear alias</button>
          <button type="button" class="btn ghost small" data-candidate-action="forced" data-test-request-id="${requestId}" data-tmdb-id="${ui.esc(tmdbId)}" ${canForce ? "" : "disabled"}>Forzar TMDb</button>
          ${forceHint ? `<small>${ui.esc(forceHint)}</small>` : ""}
        </div>
      </div>
    </details>`;
  };

  ui.resolverQuerySummary = function (item) {
    const endpoint = String(item?.endpoint || "");
    const params = item?.params && typeof item.params === "object" ? item.params : {};
    const detailMatch = endpoint.match(/^\/(movie|tv)\/(\d+)/);
    const identifierMatch = endpoint.match(/^\/find\/([^/?]+)/);
    let group = "other";
    let operation = "Otra consulta";
    let target = "TMDb";
    if (endpoint === "/search/movie") {
      group = "searches";
      operation = "Buscar película";
      target = String(params.query || "Sin título");
    } else if (endpoint === "/search/tv") {
      group = "searches";
      operation = "Buscar serie";
      target = String(params.query || "Sin título");
    } else if (detailMatch) {
      group = "details";
      operation = detailMatch[1] === "tv" ? "Ficha de serie" : "Ficha de película";
      target = `TMDb ${detailMatch[2]}`;
    } else if (identifierMatch) {
      group = "identifiers";
      operation = "Buscar identificador";
      try { target = decodeURIComponent(identifierMatch[1]); } catch (_error) { target = identifierMatch[1]; }
    }
    const year = params.year ?? params.primary_release_year ?? params.first_air_date_year;
    const settings = [
      params.language ? `Idioma ${params.language}` : "",
      params.region ? `Región ${params.region}` : "",
      year ? `Año ${year}` : ""
    ].filter(Boolean).join(" · ") || "Sin filtros adicionales";
    const status = Number(item?.status_code);
    const successful = !item?.error && status >= 200 && status < 400;
    return {
      group,
      operation,
      target,
      settings,
      successful,
      result: successful ? "Correcta" : String(item?.error || `HTTP ${item?.status_code || "sin respuesta"}`)
    };
  };

  ui.renderResolverDiagnostics = function (payload) {
    const queries = Array.isArray(payload?.queries) ? payload.queries : [];
    if (!queries.length) return "";
    const summarized = queries.map(ui.resolverQuerySummary);
    const groups = { searches: 0, details: 0, identifiers: 0, other: 0 };
    summarized.forEach(item => { groups[item.group] += 1; });
    const successful = summarized.filter(item => item.successful).length;
    const failures = summarized.length - successful;
    const queryWord = queries.length === 1 ? "consulta" : "consultas";
    const successWord = successful === 1 ? "correcta" : "correctas";
    const failureWord = failures === 1 ? "incidencia" : "incidencias";
    const groupCards = [
      ["Búsquedas", groups.searches], ["Fichas", groups.details],
      ["Identificadores", groups.identifiers], ["Otras", groups.other]
    ].filter(([, count]) => count).map(([label, count]) => `<div><span>${ui.esc(label)}</span><strong>${count}</strong></div>`).join("");
    const rows = summarized.map(item => `<tr>
      <th scope="row">${ui.esc(item.operation)}</th>
      <td>${ui.esc(item.target)}</td>
      <td>${ui.esc(item.settings)}</td>
      <td class="${item.successful ? "query-ok" : "query-fail"}">${ui.esc(item.result)}</td>
    </tr>`).join("");
    return `<details class="resolver-diagnostics">
      <summary>Diagnóstico técnico: ${queries.length} ${queryWord} · ${successful} ${successWord}${failures ? ` · ${failures} ${failureWord}` : ""}</summary>
      <div class="resolver-diagnostic-grid">${groupCards}</div>
      <div class="resolver-query-table-wrap"><table class="resolver-query-table">
        <thead><tr><th scope="col">Operación</th><th scope="col">Consulta</th><th scope="col">Ajustes</th><th scope="col">Resultado</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
      <small>Código interno: ${ui.esc(ui.resolverStatus(payload))}</small>
    </details>`;
  };

  ui.renderResolverResult = function (payload, context) {
    if (!payload) return `<div class="identity-test-empty">Aquí aparecerá el resultado completo.</div>`;
    const candidates = ui.resolverCandidates(payload);
    const candidateCards = candidates.length
      ? `<section class="resolver-candidates" aria-label="Candidatos TMDb">${candidates.map((candidate, index) => ui.renderResolverCandidate(candidate, index, context)).join("")}</section>`
      : "";
    return `${ui.renderResolverDecision(payload, candidates)}${candidateCards}${ui.renderResolverDiagnostics(payload)}`;
  };
})();
