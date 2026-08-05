(function () {
  "use strict";

  const ui = window.ArrIdentityUI;
  const RESOLVER_HUMAN_TEXT = Object.freeze({
    explicit: "Identificador explícito",
    absent: "No aportado",
    movie: "Película",
    movies: "Película",
    tv: "Serie",
    corroborated: "Título confirmado por varias fuentes",
    configured: "Coincide con un alias configurado",
    primary: "Coincide con el título principal",
    alternate: "Coincide con un título alternativo",
    none: "No coincide",
    provider_unavailable: "TMDb no está disponible; queda pendiente para reintento.",
    all_candidates_contradicted: "Todos los candidatos contradicen una evidencia obligatoria.",
    coverage_limited: "Se alcanzó el límite de cobertura y se eligió la opción plausible más probable.",
    ambiguity_adjudicated: "Había varias opciones plausibles y se eligió la más probable.",
    cached_fallback: "Se reutilizó una elección de baja confianza ya comprobada con la misma evidencia.",
    media_type_conflict: "El tipo película o serie no coincide.",
    title_conflict: "El título no coincide.",
    year_conflict: "El año no coincide.",
    runtime_class_conflict: "La duración enfrenta un cortometraje con un largometraje.",
    season_conflict: "La temporada solicitada no existe.",
    episode_conflict: "El episodio solicitado no existe.",
    multi_episode_disabled: "Los archivos con varios episodios no están permitidos.",
    absolute_episode_disabled: "La numeración absoluta no está permitida.",
    absolute_episode_conflict: "El episodio absoluto solicitado no existe.",
    special_conflict: "El especial solicitado no existe o no está permitido.",
    season_pack_conflict: "El pack de temporada no es compatible.",
    tv_season_cap: "No se pudieron comprobar todas las temporadas dentro del límite de cobertura."
  });
  const EVIDENCE_PART_LABELS = Object.freeze({
    season: "Temporada",
    numbered_episode: "Episodio",
    multi_episode: "Varios episodios",
    absolute_episode: "Episodio absoluto",
    special: "Especial",
    season_pack: "Pack de temporada"
  });
  const EVIDENCE_STATE_LABELS = Object.freeze({
    AGREE: "coincide",
    DISAGREE: "no coincide",
    UNKNOWN: "no disponible"
  });

  ui.resolverStatus = function (payload) {
    return String(payload?.decision?.status || payload?.status || payload?.error || "UNKNOWN").toUpperCase();
  };

  ui.resolverPresentation = function (payload) {
    const status = ui.resolverStatus(payload);
    const presentations = {
      ACCEPTED_CONFIDENT: {
        tone: "ok",
        title: "Aceptada",
        text: "La evidencia disponible identifica una única obra coherente."
      },
      ACCEPTED_FALLBACK: {
        tone: "ok",
        title: "Aceptada eligiendo la más probable",
        text: "No toda la evidencia ha podido confirmarse; ARR ha elegido de forma determinista la alternativa más probable."
      },
      RETRY_PROVIDER: {
        tone: "warn",
        title: "Pendiente por TMDb",
        text: "Falta información del proveedor. ARR podrá reintentarlo sin aceptar una identidad dudosa."
      },
      BLOCKED_HARD: {
        tone: "bad",
        title: "Bloqueada por contradicción",
        text: "La evidencia contiene una contradicción que impide aceptar esta identidad."
      },
      ACCEPTED: {
        tone: "ok",
        title: "Aceptada",
        text: "Esta ejecución histórica aceptó la identidad con las reglas vigentes entonces."
      },
      NO_CANDIDATES: {
        tone: "warn",
        title: "Pendiente por TMDb",
        text: "La ejecución histórica no obtuvo candidatos utilizables."
      },
      TMDB_UNAVAILABLE: {
        tone: "warn",
        title: "Pendiente por TMDb",
        text: "TMDb no estaba disponible al completar la prueba."
      },
      TMDB_ERROR: {
        tone: "warn",
        title: "Pendiente por TMDb",
        text: "TMDb no devolvió una respuesta utilizable."
      },
      INVALID_RULES: {
        tone: "bad",
        title: "Configuración no válida",
        text: "El borrador contiene un valor que el motor no puede utilizar."
      },
      PARSER_ERROR: {
        tone: "bad",
        title: "Error del Parser",
        text: "Las reglas actuales no permiten analizar este nombre."
      },
      ORCHESTRATOR_UNAVAILABLE: {
        tone: "bad",
        title: "Motor no disponible",
        text: "El panel no pudo comunicarse con el motor ARR."
      },
      INVALID_UPSTREAM_RESPONSE: {
        tone: "bad",
        title: "Respuesta del motor no válida",
        text: "El motor respondió, pero el panel no pudo interpretar el resultado."
      },
      REQUEST_ERROR: {
        tone: "bad",
        title: "Prueba no completada",
        text: "El navegador no pudo completar la petición al panel ARR."
      }
    };
    if (presentations[status]) return presentations[status];
    if (["REJECTED", "REJECTED_SCORE", "REJECTED_MARGIN"].includes(status)) {
      return {
        tone: "bad",
        title: "Bloqueada (resultado histórico)",
        text: "Esta ejecución histórica no aceptó la identidad con las reglas vigentes entonces."
      };
    }
    if (payload?.decision?.accepted === false) {
      return {
        tone: "bad",
        title: "Prueba no aceptada",
        text: "El motor terminó la prueba, pero la decisión no acepta ninguna identidad."
      };
    }
    return {
      tone: "bad",
      title: "Prueba no completada",
      text: "No se pudo obtener una decisión utilizable del motor ARR."
    };
  };

  ui.resolverCandidates = function (payload) {
    const alternatives = payload?.decision?.alternatives || payload?.alternatives;
    if (Array.isArray(alternatives)) return alternatives;
    if (Array.isArray(payload?.candidates)) return payload.candidates;
    return Array.isArray(payload?.details?.candidates) ? payload.details.candidates : [];
  };

  ui.resolverSelected = function (payload) {
    const selected = payload?.decision?.selected || payload?.selected || payload?.identity;
    return selected && typeof selected === "object" && !Array.isArray(selected) ? selected : null;
  };

  ui.normalizeResolverEvidence = function (raw) {
    const items = Array.isArray(raw)
      ? raw
      : raw && typeof raw === "object"
        ? Object.entries(raw).map(([family, value]) => (
            value && typeof value === "object" && !Array.isArray(value)
              ? { family, ...value }
              : { family, verdict: value }
          ))
        : [];
    const unique = new Map();
    items.forEach((item, index) => {
      if (!item || typeof item !== "object") return;
      const family = String(item.family || item.kind || item.name || item.source || `evidence-${index}`).trim();
      if (!family || unique.has(family)) return;
      unique.set(family, { ...item, family });
    });
    return [...unique.values()];
  };

  ui.resolverEvidence = function (payload) {
    const decision = payload?.decision || {};
    const raw = decision.evidence ?? payload?.evidence ?? decision.trace?.evidence ?? payload?.trace?.evidence;
    if (Array.isArray(raw) && raw.some(item => Array.isArray(item?.families))) {
      const selectedId = ui.resolverSelected(payload)?.tmdb_id ?? decision.selected_tmdb_id;
      const selectedEvidence = raw.find(item => String(item?.tmdb_id) === String(selectedId)) || raw[0];
      return ui.normalizeResolverEvidence(selectedEvidence?.families);
    }
    return ui.normalizeResolverEvidence(raw);
  };

  ui.resolverCounters = function (payload) {
    const decision = payload?.decision || {};
    const source = decision.phase_counts || decision.counters || payload?.counters || decision.trace?.counters || payload?.trace?.counters || {};
    const value = key => {
      const raw = source[key] ?? source[`candidates_${key}`];
      const numeric = Number(raw);
      return Number.isFinite(numeric) && numeric >= 0 ? numeric : null;
    };
    return {
      discovered: value("discovered"),
      enriched: value("enriched"),
      plausible: value("plausible"),
      eliminated: value("eliminated")
    };
  };

  ui.resolverCoverageLimited = function (payload) {
    const decision = payload?.decision || {};
    return Boolean(
      decision.coverage_limited
      ?? payload?.coverage_limited
      ?? decision.trace?.coverage_limited
      ?? payload?.trace?.coverage_limited
      ?? false
    );
  };

  ui.resolverMediaType = function (value) {
    if (value === "movie" || value === "movies") return "Película";
    if (value === "tv" || value === "series") return "Serie";
    return value ? String(value) : "Tipo sin confirmar";
  };

  ui.resolverEvidenceLabel = function (family) {
    const key = String(family || "").toLowerCase();
    const labels = {
      title: "Título",
      year: "Año",
      release: "Estreno",
      category: "Categoría",
      media_type: "Categoría",
      season: "Temporada",
      episode: "Episodio",
      runtime: "Duración",
      direct_id: "Identificador directo",
      identifiers: "Identificadores",
      original_language: "Idioma original"
    };
    return labels[key] || String(family || "Evidencia").replace(/[_-]+/g, " ");
  };

  ui.resolverHumanText = function (value) {
    const text = String(value ?? "").trim();
    if (!text) return "";
    return RESOLVER_HUMAN_TEXT[text.toLowerCase()] || text.replace(/[_-]+/g, " ");
  };

  ui.resolverCompactValues = function (value) {
    const values = Array.isArray(value) ? value : [value];
    return values
      .filter(item => item !== null && item !== undefined && item !== "")
      .slice(0, 12)
      .map(item => ui.resolverHumanText(item))
      .join(", ");
  };

  ui.resolverEvidenceDetail = function (item) {
    const value = item?.detail ?? item?.message ?? item?.reason ?? item?.value ?? item?.observed;
    if (value === null || value === undefined || value === "") return "";
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
      return ui.resolverHumanText(value);
    }
    if (Array.isArray(value)) return ui.resolverCompactValues(value);
    if (typeof value !== "object") return ui.resolverHumanText(value);
    const family = String(item?.family || "").toLowerCase();
    if (family === "year") {
      const expected = ui.resolverCompactValues(value.expected);
      const candidate = ui.resolverCompactValues(value.candidate);
      return [expected ? `Esperado: ${expected}` : "", candidate ? `TMDb: ${candidate}` : ""].filter(Boolean).join(" · ");
    }
    if (family === "runtime") {
      const observed = ui.resolverCompactValues(value.observed_minutes);
      const candidate = ui.resolverCompactValues(value.candidate_minutes);
      return [observed ? `Archivo: ${observed} min` : "", candidate ? `TMDb: ${candidate} min` : ""].filter(Boolean).join(" · ");
    }
    if (family === "episode" && Array.isArray(value.subchecks)) {
      return value.subchecks.map(check => {
        const label = EVIDENCE_PART_LABELS[String(check?.name || "")] || ui.resolverHumanText(check?.name || "Comprobación");
        const state = EVIDENCE_STATE_LABELS[String(check?.state || "UNKNOWN").toUpperCase()] || "no disponible";
        return `${label}: ${state}`;
      }).join(" · ");
    }
    const labels = { expected: "Esperado", candidate: "TMDb", checked: "Comprobados", count: "Cantidad", episodes: "Episodios", present: "Presente" };
    return Object.entries(value).flatMap(([key, raw]) => {
      if (raw && typeof raw === "object" && !Array.isArray(raw)) return [];
      const detail = ui.resolverCompactValues(raw);
      return detail ? [`${labels[key] || ui.resolverHumanText(key)}: ${detail}`] : [];
    }).slice(0, 4).join(" · ");
  };

  ui.resolverDomId = function (...parts) {
    return parts.join("-").replace(/[^a-z0-9_-]+/gi, "-").replace(/^-+|-+$/g, "").toLowerCase();
  };

  ui.renderResolverEvidence = function (evidence, idBase = "resolver-evidence-overview") {
    if (!evidence.length) return `<p class="resolver-evidence-empty">No hay evidencias desglosadas en este resultado histórico.</p>`;
    const titleId = `${ui.resolverDomId(idBase)}-title`;
    const rows = evidence.map(item => {
      const rawVerdict = String(item.state ?? item.verdict ?? "").toUpperCase();
      const verdict = ["AGREE", "DISAGREE", "UNKNOWN"].includes(rawVerdict)
        ? rawVerdict
        : "UNKNOWN";
      const verdictLabel = {
        AGREE: "Coincide",
        DISAGREE: "No coincide",
        UNKNOWN: "No disponible"
      }[verdict];
      const detail = ui.resolverEvidenceDetail(item);
      return `<li class="resolver-evidence-item ${verdict.toLowerCase()}">
        <div><strong>${ui.esc(ui.resolverEvidenceLabel(item.family))}</strong>${detail ? `<small>${ui.esc(detail)}</small>` : ""}</div>
        <span>${ui.esc(verdictLabel)}</span>
      </li>`;
    }).join("");
    return `<section class="resolver-evidence" aria-labelledby="${ui.esc(titleId)}">
      <div class="resolver-section-heading"><h5 id="${ui.esc(titleId)}">Evidencias</h5><small>Una conclusión por familia</small></div>
      <ul>${rows}</ul>
    </section>`;
  };

  ui.renderResolverCounters = function (payload) {
    const counters = ui.resolverCounters(payload);
    const cards = [
      ["Descubiertos", counters.discovered],
      ["Enriquecidos", counters.enriched],
      ["Plausibles", counters.plausible],
      ["Eliminados", counters.eliminated]
    ].map(([label, value]) => `<div><span>${ui.esc(label)}</span><strong>${value === null ? "—" : ui.esc(value)}</strong></div>`).join("");
    return `<section class="resolver-funnel" aria-label="Recorrido de candidatos">${cards}</section>`;
  };

  ui.resolverConcreteCause = function (payload) {
    const decision = payload?.decision || {};
    const details = payload?.details || {};
    const value = decision.reason_message
      ?? decision.message
      ?? decision.fallback_reason
      ?? details.reason_message
      ?? details.reason
      ?? payload?.message;
    return typeof value === "string" ? ui.resolverHumanText(value) : "";
  };

  ui.renderResolverDecision = function (payload) {
    const presentation = ui.resolverPresentation(payload);
    const concreteCause = ui.resolverConcreteCause(payload);
    const limited = ui.resolverCoverageLimited(payload);
    return `<section class="resolver-outcome ${ui.esc(presentation.tone)}" aria-labelledby="resolver-outcome-title">
      <small>RESULTADO</small>
      <h4 id="resolver-outcome-title">${ui.esc(presentation.title)}</h4>
      <p>${ui.esc(presentation.text)}</p>
      ${concreteCause ? `<p class="resolver-concrete-cause"><strong>Motivo:</strong> ${ui.esc(concreteCause)}</p>` : ""}
      ${limited ? `<div class="resolver-coverage-limited"><strong>Cobertura limitada</strong><span>No se revisó todo el universo posible de TMDb; la decisión refleja la evidencia alcanzable.</span></div>` : ""}
    </section>`;
  };

  ui.resolverCandidateIdentity = function (candidate) {
    return {
      tmdbId: candidate?.tmdb_id ?? candidate?.selected_tmdb_id ?? candidate?.id ?? "-",
      title: candidate?.title || candidate?.name || "Sin título",
      year: candidate?.year ?? candidate?.release_year ?? candidate?.first_air_year ?? "Sin año",
      mediaType: candidate?.media_type || candidate?.category || candidate?.type || ""
    };
  };

  ui.renderResolverCandidateActions = function (candidate, context) {
    const identity = ui.resolverCandidateIdentity(candidate);
    const category = ["movies", "tv"].includes(context?.category) ? context.category : "";
    const parserTitle = String(context?.parserTitle || "").trim();
    const parserYear = String(context?.parserYear || "").trim();
    const candidateTitle = String(identity.title || "").trim();
    const tmdbId = String(identity.tmdbId === "-" ? "" : identity.tmdbId).trim();
    const requestId = Number(context?.requestId || 0);
    const locked = Boolean(ui.state.readOnly || ui.state.resetting);
    const canAlias = Boolean(!locked && category && parserTitle && candidateTitle);
    const canForce = Boolean(!locked && category && parserTitle && tmdbId && (category === "tv" || parserYear));
    return `<div class="resolver-candidate-actions">
      <button type="button" class="btn ghost small" data-candidate-action="alias" data-test-request-id="${requestId}" data-tmdb-id="${ui.esc(tmdbId)}" data-candidate-title="${ui.esc(candidateTitle)}" title="Se añadirá al borrador Común" ${canAlias ? "" : "disabled"}>Crear alias común</button>
      <button type="button" class="btn ghost small" data-candidate-action="forced" data-test-request-id="${requestId}" data-tmdb-id="${ui.esc(tmdbId)}" title="Se añadirá al borrador Común" ${canForce ? "" : "disabled"}>Forzar TMDb en Común</button>
    </div>`;
  };

  ui.renderResolverSelection = function (selected, context) {
    if (!selected) return "";
    const identity = ui.resolverCandidateIdentity(selected);
    return `<section class="resolver-selection" aria-labelledby="resolver-selection-title">
      <div><small>IDENTIDAD ELEGIDA</small><h5 id="resolver-selection-title">${ui.esc(identity.title)}</h5></div>
      <dl><div><dt>TMDb</dt><dd>${ui.esc(identity.tmdbId)}</dd></div><div><dt>Año</dt><dd>${ui.esc(identity.year)}</dd></div><div><dt>Tipo</dt><dd>${ui.esc(ui.resolverMediaType(identity.mediaType))}</dd></div></dl>
      ${ui.renderResolverCandidateActions(selected, context)}
    </section>`;
  };

  ui.renderResolverCandidate = function (candidate, index, context) {
    const identity = ui.resolverCandidateIdentity(candidate);
    const requestId = Number(context?.requestId || 0);
    const candidateId = ui.resolverDomId("resolver-candidate", requestId, index, identity.tmdbId);
    const candidateEvidence = ui.normalizeResolverEvidence(candidate?.evidence);
    const eliminated = candidate?.eliminated === true;
    const eliminationReasons = Array.isArray(candidate?.elimination_reasons)
      ? candidate.elimination_reasons.filter(Boolean)
      : [];
    const notes = Array.isArray(candidate?.matching_rules)
      ? [...new Set(candidate.matching_rules.map(item => typeof item === "string" ? item : item?.detail).filter(Boolean))]
      : [];
    return `<details id="${ui.esc(candidateId)}" class="resolver-candidate ${index === 0 ? "top" : ""}">
      <summary>
        <span class="resolver-rank">${index + 1}.º</span>
        <span class="resolver-candidate-name"><strong>${ui.esc(identity.title)}</strong><small>TMDb ${ui.esc(identity.tmdbId)} · ${ui.esc(ui.resolverMediaType(identity.mediaType))}</small></span>
        <span class="resolver-year">${ui.esc(identity.year)}</span>
        <span class="resolver-candidate-state ${eliminated ? "eliminated" : "plausible"}">${eliminated ? "Eliminada" : "Plausible"}</span>
        <span class="resolver-open-label">Ver detalles</span>
      </summary>
      <div class="resolver-candidate-body">
        ${candidateEvidence.length ? ui.renderResolverEvidence(candidateEvidence, `${candidateId}-evidence`) : ""}
        ${eliminationReasons.length ? `<section class="resolver-candidate-notes eliminated"><strong>Contradicciones</strong><ul>${eliminationReasons.map(reason => `<li>${ui.esc(ui.resolverHumanText(reason))}</li>`).join("")}</ul></section>` : ""}
        ${notes.length ? `<section class="resolver-candidate-notes"><strong>Señales observadas</strong><ul>${notes.map(note => `<li>${ui.esc(ui.resolverHumanText(note))}</li>`).join("")}</ul></section>` : ""}
        ${ui.renderResolverCandidateActions(candidate, context)}
      </div>
    </details>`;
  };

  ui.renderResolverAlternatives = function (alternatives, context) {
    if (!alternatives.length) return "";
    return `<section class="resolver-alternatives" aria-labelledby="resolver-alternatives-title">
      <div class="resolver-section-heading"><h5 id="resolver-alternatives-title">Alternativas</h5><small>Ordenadas de más a menos probable</small></div>
      <div class="resolver-candidates">${alternatives.map((candidate, index) => ui.renderResolverCandidate(candidate, index, context)).join("")}</div>
    </section>`;
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
    const selected = ui.resolverSelected(payload);
    const selectedId = selected?.tmdb_id ?? payload?.decision?.selected_tmdb_id;
    const alternatives = ui.resolverCandidates(payload).filter(candidate => (
      selectedId === null || selectedId === undefined || String(candidate?.tmdb_id) !== String(selectedId)
    ));
    return `${ui.renderResolverDecision(payload)}${ui.renderResolverCounters(payload)}${ui.renderResolverSelection(selected, context)}${ui.renderResolverEvidence(ui.resolverEvidence(payload), "resolver-evidence-overview")}${ui.renderResolverAlternatives(alternatives, context)}${ui.renderResolverDiagnostics(payload)}`;
  };
})();
