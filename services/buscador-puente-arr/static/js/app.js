const form = document.getElementById("searchForm");
const queryInput = document.getElementById("query");
const categoryInput = document.getElementById("category");
const trackerButton = document.getElementById("trackerButton");
const trackerPanel = document.getElementById("trackerPanel");
const trackerSearch = document.getElementById("trackerSearch");
const trackerList = document.getElementById("trackerList");
const trackerAll = document.getElementById("trackerAll");
const trackerNone = document.getElementById("trackerNone");
const sortMode = document.getElementById("sortMode");
const categoryButton = document.getElementById("categoryButton");
const sortGbButton = document.getElementById("sortGbButton");
const sortAzButton = document.getElementById("sortAzButton");
const seedMinButton = document.getElementById("seedMinButton");
const peerMinButton = document.getElementById("peerMinButton");
const statusBox = document.getElementById("status");
const resultsBox = document.getElementById("results");
const searchBtn = document.getElementById("searchBtn");
const clearSearchBtn = document.getElementById("clearSearchBtn");
const settingsToggle = document.getElementById("settingsToggle");
const mainView = document.getElementById("mainView");
const settingsView = document.getElementById("settingsView");
const saveSettingsBtn = document.getElementById("saveSettings");
const saveSettingsQbitBtn = document.getElementById("saveSettingsQbit");
const cancelSettingsBtn = document.getElementById("cancelSettings");
const testClassifyBtn = document.getElementById("testClassify");
const classifyTitle = document.getElementById("classifyTitle");
const classifyResult = document.getElementById("classifyResult");
const testRdtBtn = document.getElementById("testRdt");
const testQbitBtn = document.getElementById("testQbit");

const fields = {
  rdtStartTimeout: document.getElementById("rdtStartTimeout"),
  rdRetries: document.getElementById("rdRetries"),
  rdtReadyTimeout: document.getElementById("rdtReadyTimeout"),
  rdtFallback: document.getElementById("rdtFallback"),
  rdtCleanup: document.getElementById("rdtCleanup"),
  qbitFallback: document.getElementById("qbitFallback"),
  qbitPaused: document.getElementById("qbitPaused"),
  qbitDefaultCategory: document.getElementById("qbitDefaultCategory"),
  autoUncertainCategory: document.getElementById("autoUncertainCategory"),
  seriesTemplates: document.getElementById("seriesTemplates"),
  seriesWords: document.getElementById("seriesWords"),
  movieWords: document.getElementById("movieWords"),
  movieYears: document.getElementById("movieYears")
};

queryInput.value = localStorage.getItem("arr_query") || "";
categoryInput.value = localStorage.getItem("arr_category") || "";
sortMode.value = localStorage.getItem("arr_sort") || "size_desc";

let lastResults = [];
let indexers = [];
let trackerMode = localStorage.getItem("arr_trackers_mode") || "all";
let selectedTrackers = new Set(readSavedTrackers());
let minSeeders = readSavedNumber("arr_min_seeders", 1);
let minPeers = readSavedNumber("arr_min_peers", 0);
const PAGE_SIZE = 30;
const DONE_BADGE_AUTO_CLEAR_MS = 2 * 60 * 1000;
let currentPage = Math.max(1, readSavedNumber("arr_page", 1));
let searchTimer = 0;
let searchPollTimer = 0;
let activeSearchJobId = localStorage.getItem("arr_search_job") || "";
let searchSoundJobId = "";
let restoreScrollPending = true;
let rememberedScrollY = readSavedNumber("arr_scroll_y", 0);
let sendJobs = readSavedObject("arr_send_jobs");
let sendClearTimers = {};
let settings = null;
let defaultSettings = null;
let finishSound = null;
const finishSoundUrl = "/static/sounds/applepay.mp3";
const finishSoundVolume = 0.55;

function getFinishSound() {
  if (!finishSound) {
    finishSound = new Audio(finishSoundUrl);
    finishSound.preload = "auto";
    finishSound.volume = finishSoundVolume;
  }
  return finishSound;
}

function prepareFinishSound() {
  try {
    const audio = getFinishSound();
    audio.pause();
    audio.currentTime = 0;
    audio.muted = true;
    const promise = audio.play();
    if (promise && promise.then) {
      promise.then(() => {
        audio.pause();
        audio.currentTime = 0;
        audio.muted = false;
        audio.volume = finishSoundVolume;
      }).catch(() => {
        audio.muted = false;
        audio.volume = finishSoundVolume;
        audio.load();
      });
    } else {
      audio.pause();
      audio.currentTime = 0;
      audio.muted = false;
      audio.volume = finishSoundVolume;
    }
  } catch (error) {}
}

function playFinishSound() {
  try {
    const audio = getFinishSound();
    audio.pause();
    audio.currentTime = 0;
    audio.muted = false;
    audio.volume = finishSoundVolume;
    const promise = audio.play();
    if (promise && promise.catch) promise.catch(() => {});
  } catch (error) {}
}

function readSavedTrackers() {
  const raw = localStorage.getItem("arr_trackers");
  if (!raw) {
    const oldValue = localStorage.getItem("arr_tracker") || "";
    return oldValue ? [oldValue] : [];
  }
  try {
    const value = JSON.parse(raw);
    if (Array.isArray(value)) return value;
  } catch (_error) {
    return [];
  }
  return [];
}

function readSavedNumber(key, fallback) {
  const value = Number(localStorage.getItem(key));
  return Number.isFinite(value) && value >= 0 ? Math.floor(value) : fallback;
}

function saveNumber(key, value) {
  localStorage.setItem(key, String(Math.max(0, Math.floor(numberValue(value)))));
}

function readSavedObject(key) {
  try {
    const value = JSON.parse(localStorage.getItem(key) || "{}");
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  } catch (_error) {
    return {};
  }
}

function newJobId() {
  if (window.crypto && typeof window.crypto.randomUUID === "function") {
    return window.crypto.randomUUID();
  }
  return `job_${Date.now()}_${Math.random().toString(16).slice(2)}`;
}

function saveViewState() {
  localStorage.setItem("arr_query", queryInput.value);
  saveNumber("arr_page", currentPage);
  if (!restoreScrollPending) {
    rememberedScrollY = window.scrollY;
    saveNumber("arr_scroll_y", rememberedScrollY);
  }
}

function restoreSavedScroll() {
  if (!restoreScrollPending) return;
  restoreScrollPending = false;
  if (rememberedScrollY > 0) setTimeout(() => window.scrollTo(0, rememberedScrollY), 60);
}

function saveSendJobs() {
  const entries = Object.entries(sendJobs)
    .filter(([, entry]) => typeof entry === "string" || (entry && entry.jobId))
    .slice(-200);
  sendJobs = Object.fromEntries(entries);
  localStorage.setItem("arr_send_jobs", JSON.stringify(sendJobs));
}

function sendJobIdFor(itemId) {
  const entry = sendJobs[itemId];
  if (!entry) return "";
  return typeof entry === "string" ? entry : String(entry.jobId || "");
}

function rememberSendJob(itemId, jobId, state = "") {
  if (!itemId || !jobId) return;
  sendJobs[itemId] = {
    jobId,
    state,
    updatedAt: Date.now()
  };
  saveSendJobs();
}

function forgetSendJob(itemId, jobId = "") {
  const current = sendJobIdFor(itemId);
  if (!current || (jobId && current !== jobId)) return false;
  delete sendJobs[itemId];
  saveSendJobs();
  return true;
}

function status(text) {
  statusBox.textContent = text || "";
}

function shortError(text) {
  const clean = String(text || "error").replace(/magnet:\?[^\s]+/g, "magnet").replace(/\s+/g, " ").trim();
  return clean.length > 180 ? `${clean.slice(0, 177)}...` : clean;
}

function setActiveSearchJob(jobId) {
  activeSearchJobId = jobId || "";
  if (activeSearchJobId) {
    localStorage.setItem("arr_search_job", activeSearchJobId);
  } else {
    localStorage.removeItem("arr_search_job");
  }
}

function clearSearchMemory() {
  clearTimeout(searchTimer);
  clearTimeout(searchPollTimer);
  setActiveSearchJob("");
  searchSoundJobId = "";
  lastResults = [];
  currentPage = 1;
  rememberedScrollY = 0;
  restoreScrollPending = false;
  queryInput.value = "";
  searchBtn.disabled = false;
  resultsBox.textContent = "";
  localStorage.removeItem("arr_query");
  localStorage.removeItem("arr_search_job");
  localStorage.removeItem("arr_page");
  localStorage.removeItem("arr_scroll_y");
  status("Listo");
  window.scrollTo(0, 0);
  queryInput.focus();
}

function scheduleSearchPoll(jobId) {
  clearTimeout(searchPollTimer);
  searchPollTimer = setTimeout(() => pollSearchJob(jobId), 900);
}

function applySearchJob(job) {
  if (!job || job.id !== activeSearchJobId) return;
  if (job.request && job.request.query) {
    queryInput.value = job.request.query;
    localStorage.setItem("arr_query", job.request.query);
  }
  if (job.state === "queued" || job.state === "running") {
    searchBtn.disabled = true;
    status("Buscando...");
    scheduleSearchPoll(job.id);
    return;
  }

  clearTimeout(searchPollTimer);
  searchBtn.disabled = false;
  if (job.state === "done") {
    lastResults = job.result && Array.isArray(job.result.results) ? job.result.results : [];
    render(lastResults);
    restoreSavedScroll();
    if (searchSoundJobId === job.id) {
      searchSoundJobId = "";
      playFinishSound();
    }
    return;
  }
  status(`Error: ${shortError(job.error || "fallo buscando")}`);
}

async function pollSearchJob(jobId) {
  if (!jobId || jobId !== activeSearchJobId) return;
  try {
    const response = await fetch(`/api/jobs/search/${encodeURIComponent(jobId)}`);
    const data = await response.json();
    if (response.status === 404) {
      setActiveSearchJob("");
      searchBtn.disabled = false;
      status("Listo");
      return;
    }
    if (!response.ok || !data.ok) throw new Error(data.error || "fallo recuperando búsqueda");
    applySearchJob(data.job);
  } catch (error) {
    status(`Reconectando búsqueda: ${shortError(error.message || error)}`);
    scheduleSearchPoll(jobId);
  }
}

async function startSearchJob(q) {
  const jobId = newJobId();
  setActiveSearchJob(jobId);
  localStorage.setItem("arr_query", q);
  currentPage = 1;
  saveNumber("arr_page", currentPage);
  restoreScrollPending = false;
  rememberedScrollY = 0;
  saveNumber("arr_scroll_y", 0);
  clearTimeout(searchPollTimer);
  prepareFinishSound();
  searchSoundJobId = jobId;
  searchBtn.disabled = true;
  status("Buscando...");
  resultsBox.textContent = "";

  try {
    const ids = effectiveTrackerIds();
    const response = await fetch("/api/jobs/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        job_id: jobId,
        query: q,
        category: categoryInput.value || "auto",
        indexers: ids
      })
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || "fallo buscando");
    if (activeSearchJobId !== jobId) return;
    if (data.job.id !== activeSearchJobId) {
      setActiveSearchJob(data.job.id);
      searchSoundJobId = data.job.id;
    }
    applySearchJob(data.job);
  } catch (error) {
    status(`Comprobando búsqueda: ${shortError(error.message || error)}`);
    scheduleSearchPoll(jobId);
  }
}

function linesToText(value) {
  return Array.isArray(value) ? value.join("\n") : "";
}

function textToLines(value) {
  return String(value || "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
}

function numberValue(value) {
  const n = Number(value || 0);
  return Number.isFinite(n) ? n : 0;
}

function categoryLabel(value) {
  if (value === "movies") return "Películas";
  if (value === "tv") return "Series";
  if (value === "manual") return "Manual";
  return "Auto";
}

function updateCategoryButton() {
  categoryButton.textContent = categoryLabel(categoryInput.value);
}

function cycleCategory() {
  const order = ["", "movies", "tv", "manual"];
  const next = order[(order.indexOf(categoryInput.value) + 1) % order.length];
  categoryInput.value = next;
  localStorage.setItem("arr_category", next);
  updateCategoryButton();
}

function updateSortButtons() {
  const sort = sortMode.value;
  sortGbButton.textContent = sort === "size_asc" ? "GB ↑" : "GB ↓";
  sortAzButton.textContent = sort === "tracker_desc" ? "Z-A" : "A-Z";
  sortGbButton.classList.toggle("is-active", sort === "size_desc" || sort === "size_asc");
  sortAzButton.classList.toggle("is-active", sort === "tracker_asc" || sort === "tracker_desc");
}

function updateLimitButtons() {
  seedMinButton.textContent = `Semillas ≥ ${minSeeders}`;
  peerMinButton.textContent = `Pares ≥ ${minPeers}`;
}

function askMinimum(label, currentValue) {
  const raw = window.prompt(`${label} mínimo`, String(currentValue));
  if (raw === null) return currentValue;
  const value = Number(raw.replace(",", "."));
  if (!Number.isFinite(value) || value < 0) return currentValue;
  return Math.floor(value);
}

function showSettings(open) {
  mainView.hidden = open;
  resultsBox.hidden = open;
  settingsView.hidden = !open;
  settingsToggle.classList.toggle("is-active", open);
  settingsToggle.setAttribute("aria-label", open ? "Volver a búsqueda" : "Abrir ajustes");
  localStorage.setItem("arr_view", open ? "settings" : "main");
  if (open) {
    status("Ajustes del motor");
  } else if (lastResults.length) {
    status(`${filteredAndSorted(lastResults).length} resultados`);
  } else {
    status("Listo");
  }
}

async function loadSettings() {
  try {
    const response = await fetch("/api/settings");
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || "fallo ajustes");
    settings = data.settings;
    defaultSettings = data.defaults;
    renderSettings(settings);
  } catch (error) {
    status(`Error ajustes: ${shortError(error.message || error)}`);
  }
}

function renderSettings(value) {
  if (!value) return;
  fields.rdtStartTimeout.value = value.rdt.start_timeout_sec;
  fields.rdRetries.value = value.rdt.rd_retry_attempts;
  fields.rdtReadyTimeout.value = value.rdt.ready_timeout_sec;
  fields.rdtFallback.checked = value.rdt.fallback_enabled;
  fields.rdtCleanup.checked = value.rdt.cleanup_on_fallback;
  fields.qbitFallback.checked = value.qbit.fallback_enabled;
  fields.qbitPaused.checked = value.qbit.add_paused;
  fields.qbitDefaultCategory.value = value.qbit.default_category;
  fields.autoUncertainCategory.value = value.qbit.auto_uncertain_category;
  fields.seriesTemplates.value = linesToText(value.auto.series_templates);
  fields.seriesWords.value = linesToText(value.auto.series_words);
  fields.movieWords.value = linesToText(value.auto.movie_words);
  fields.movieYears.checked = value.auto.movie_years;
}

function collectSettings() {
  return {
    rdt: {
      start_timeout_sec: numberValue(fields.rdtStartTimeout.value),
      rd_retry_attempts: numberValue(fields.rdRetries.value),
      ready_timeout_sec: numberValue(fields.rdtReadyTimeout.value),
      fallback_enabled: fields.rdtFallback.checked,
      cleanup_on_fallback: fields.rdtCleanup.checked
    },
    qbit: {
      fallback_enabled: fields.qbitFallback.checked,
      add_paused: fields.qbitPaused.checked,
      default_category: fields.qbitDefaultCategory.value,
      auto_uncertain_category: fields.autoUncertainCategory.value
    },
    auto: {
      series_templates: textToLines(fields.seriesTemplates.value),
      series_words: textToLines(fields.seriesWords.value),
      movie_words: textToLines(fields.movieWords.value),
      movie_years: fields.movieYears.checked
    }
  };
}

async function saveSettings() {
  try {
    const response = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings: collectSettings() })
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || "fallo guardando");
    settings = data.settings;
    renderSettings(settings);
    status("Ajustes guardados");
  } catch (error) {
    status(`Error ajustes: ${shortError(error.message || error)}`);
  }
}

async function resetSettings(scope) {
  if (scope === "auto" && defaultSettings && settings) {
    settings.auto = JSON.parse(JSON.stringify(defaultSettings.auto));
    renderSettings(settings);
    status("Auto restaurado");
    return;
  }
  try {
    const response = await fetch("/api/settings/reset", { method: "POST" });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || "fallo restaurando");
    settings = data.settings;
    renderSettings(settings);
    status("Valores seguros restaurados");
  } catch (error) {
    status(`Error ajustes: ${shortError(error.message || error)}`);
  }
}

async function classifyProbe() {
  const title = classifyTitle.value.trim();
  if (!title) {
    classifyResult.textContent = "Escribe un título";
    return;
  }
  try {
    const response = await fetch("/api/classify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, settings: collectSettings() })
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || "fallo clasificando");
    const label = data.category === "tv" ? "Series" : data.category === "movies" ? "Películas" : "Manual";
    classifyResult.textContent = label;
  } catch (error) {
    classifyResult.textContent = shortError(error.message || error);
  }
}

async function testEndpoint(path, label) {
  try {
    const response = await fetch(path, { method: "POST" });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || "fallo test");
    status(`${label}: OK`);
  } catch (error) {
    status(`${label}: ${shortError(error.message || error)}`);
  }
}

async function loadIndexers() {
  try {
    const response = await fetch("/api/indexers");
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || "fallo cargando trackers");
    indexers = data.indexers || [];
    selectedTrackers = new Set([...selectedTrackers].filter((id) => indexers.some((item) => item.id === id)));
    if (trackerMode !== "custom") trackerMode = "all";
    saveTrackers();
    renderTrackerList();
    updateTrackerButton();
  } catch (error) {
    status(`Error trackers: ${shortError(error.message || error)}`);
  }
}

function saveTrackers() {
  localStorage.setItem("arr_trackers_mode", trackerMode);
  localStorage.setItem("arr_trackers", JSON.stringify([...selectedTrackers]));
}

function effectiveTrackerIds() {
  if (trackerMode !== "custom") return [];
  return [...selectedTrackers];
}

function trackerIsChecked(id) {
  return trackerMode !== "custom" || selectedTrackers.has(id);
}

function updateTrackerButton() {
  if (trackerMode !== "custom") {
    trackerButton.textContent = "Trackers: Todos";
    return;
  }
  if (!selectedTrackers.size) {
    trackerButton.textContent = "Trackers: Ninguno";
    return;
  }
  if (selectedTrackers.size === 1) {
    const item = indexers.find((row) => selectedTrackers.has(row.id));
    trackerButton.textContent = item ? item.title : "Trackers: 1";
    return;
  }
  trackerButton.textContent = `Trackers: ${selectedTrackers.size}`;
}

function renderTrackerList() {
  const needle = trackerSearch.value.trim().toLowerCase();
  trackerList.textContent = "";
  for (const item of indexers) {
    if (needle && !item.title.toLowerCase().includes(needle)) continue;
    const label = document.createElement("label");
    label.className = "tracker-option";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = trackerIsChecked(item.id);
    checkbox.addEventListener("change", () => {
      if (trackerMode !== "custom") {
        trackerMode = "custom";
        selectedTrackers = new Set(indexers.map((row) => row.id));
      }
      if (checkbox.checked) {
        selectedTrackers.add(item.id);
      } else {
        selectedTrackers.delete(item.id);
      }
      if (selectedTrackers.size === indexers.length) {
        trackerMode = "all";
        selectedTrackers.clear();
      }
      saveTrackers();
      renderTrackerList();
      updateTrackerButton();
    });
    const text = document.createElement("span");
    text.textContent = item.title;
    label.append(checkbox, text);
    trackerList.appendChild(label);
  }
}

function openTrackerPanel(open) {
  trackerPanel.hidden = !open;
  trackerButton.setAttribute("aria-expanded", open ? "true" : "false");
  if (open) trackerSearch.focus();
}

function scheduleSearch() {
  clearTimeout(searchTimer);
  if (queryInput.value.trim()) {
    searchTimer = setTimeout(() => form.requestSubmit(), 180);
  } else {
    resetPageAndRender(lastResults);
  }
}

function metaText(item) {
  const parts = [];
  if (item.tracker) parts.push(item.tracker);
  if (item.size_text) parts.push(item.size_text);
  if (item.seeders) parts.push(`${item.seeders} semillas`);
  if (item.peers) parts.push(`${item.peers} pares`);
  return parts;
}

function setCardState(row, text, state) {
  const badge = row.querySelector(".state-badge");
  row.classList.remove("is-sending", "is-queued", "is-error");
  if (state) row.classList.add(state);
  if (badge) badge.textContent = text || "";
}

function resetCard(row) {
  row.classList.remove("is-sending", "is-queued", "is-error");
  row.dataset.busy = "0";
  const badge = row.querySelector(".state-badge");
  if (badge) badge.remove();
}

function clearFinalSendTimer(itemId) {
  if (sendClearTimers[itemId]) {
    clearTimeout(sendClearTimers[itemId]);
    delete sendClearTimers[itemId];
  }
}

function dismissSendJob(jobId) {
  if (!jobId) return;
  fetch(`/api/jobs/download/${encodeURIComponent(jobId)}/dismiss`, { method: "POST" }).catch(() => {});
}

function clearSendState(item, row, jobId, announce = true) {
  clearFinalSendTimer(item.id);
  forgetSendJob(item.id, jobId);
  resetCard(row);
  dismissSendJob(jobId);
  if (announce) status("Marca quitada");
}

function scheduleDoneBadgeClear(item, row, jobId) {
  clearFinalSendTimer(item.id);
  sendClearTimers[item.id] = setTimeout(() => {
    if (!row.isConnected || sendJobIdFor(item.id) !== jobId) return;
    clearSendState(item, row, jobId, false);
  }, DONE_BADGE_AUTO_CLEAR_MS);
}

function showCardBadge(row, text, options = {}) {
  let badge = row.querySelector(".state-badge");
  const tag = options.clearable ? "button" : "span";
  if (badge && badge.tagName.toLowerCase() !== tag) {
    badge.remove();
    badge = null;
  }
  if (!badge) {
    badge = document.createElement(tag);
    badge.className = "state-badge";
    if (tag === "button") badge.type = "button";
    row.querySelector(".meta").appendChild(badge);
  }
  badge.textContent = text;
  badge.title = options.title || "";
  badge.setAttribute("aria-label", options.ariaLabel || text || "");
  if (options.clearable) {
    badge.classList.add("is-clearable");
    badge.onclick = (event) => {
      event.preventDefault();
      event.stopPropagation();
      options.onClear();
    };
    badge.onkeydown = (event) => event.stopPropagation();
  } else {
    badge.classList.remove("is-clearable");
    badge.onclick = null;
    badge.onkeydown = null;
  }
}

function applySendJob(job, item, row, announce = false) {
  if (!job || !row.isConnected) return;
  row.dataset.busy = "1";
  rememberSendJob(item.id, job.id, job.state);
  showCardBadge(row, "");
  if (job.state === "queued" || job.state === "running") {
    clearFinalSendTimer(item.id);
    setCardState(row, "Enviando...", "is-sending");
    if (announce) status(`Enviando: ${item.title}`);
    setTimeout(() => pollSendJob(job.id, item, row, announce), 900);
    return;
  }
  if (job.state === "done") {
    const category = job.result && job.result.category;
    const label = category === "tv" ? "En cola TV" : category === "movies" ? "En cola Pelis" : "En cola";
    showCardBadge(row, label, {
      clearable: true,
      title: "Quitar marca",
      ariaLabel: `Quitar marca ${label}`,
      onClear: () => clearSendState(item, row, job.id)
    });
    setCardState(row, label, "is-queued");
    scheduleDoneBadgeClear(item, row, job.id);
    if (announce) status(`Enviado: ${item.title} -> ${category || "auto"}`);
    return;
  }
  clearFinalSendTimer(item.id);
  showCardBadge(row, "Error", {
    clearable: true,
    title: "Quitar error",
    ariaLabel: "Quitar error de esta tarjeta",
    onClear: () => clearSendState(item, row, job.id)
  });
  setCardState(row, "Error", "is-error");
  if (announce) status(`Error: ${shortError(job.error || "fallo al enviar")}`);
}

async function pollSendJob(jobId, item, row, announce = false) {
  if (!jobId || !row.isConnected) return;
  try {
    const response = await fetch(`/api/jobs/download/${encodeURIComponent(jobId)}`);
    const data = await response.json();
    if (response.status === 404) {
      forgetSendJob(item.id, jobId);
      resetCard(row);
      return;
    }
    if (!response.ok || !data.ok) throw new Error(data.error || "fallo recuperando envío");
    applySendJob(data.job, item, row, announce);
  } catch (error) {
    if (announce) status(`Reconectando envío: ${shortError(error.message || error)}`);
    setTimeout(() => pollSendJob(jobId, item, row, announce), 1200);
  }
}

function restoreSendState(item, row) {
  const jobId = sendJobIdFor(item.id);
  if (jobId) pollSendJob(jobId, item, row, false);
}

async function send(item, row) {
  const rememberedJobId = sendJobIdFor(item.id);
  if (rememberedJobId) {
    status(`Recuperando envío: ${item.title}`);
    pollSendJob(rememberedJobId, item, row, true);
    return;
  }
  if (row.dataset.busy === "1") return;
  const jobId = newJobId();
  rememberSendJob(item.id, jobId, "queued");
  row.dataset.busy = "1";
  showCardBadge(row, "Enviando...");
  setCardState(row, "Enviando...", "is-sending");
  status(`Enviando: ${item.title}`);
  try {
    const response = await fetch("/api/jobs/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        job_id: jobId,
        result_id: item.id,
        result: item,
        category: categoryInput.value
      })
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || "fallo al enviar");
    if (data.job.id !== jobId) {
      rememberSendJob(item.id, data.job.id, data.job.state);
    }
    applySendJob(data.job, item, row, true);
  } catch (error) {
    status(`Comprobando envío: ${shortError(error.message || error)}`);
    setTimeout(() => pollSendJob(jobId, item, row, true), 1200);
  }
}

function pageSequence(pageCount) {
  if (pageCount <= 6) {
    return Array.from({ length: pageCount }, (_item, index) => index + 1);
  }
  const pages = new Set([1, pageCount]);
  if (currentPage <= 3) {
    pages.add(2);
    pages.add(3);
  } else if (currentPage >= pageCount - 2) {
    pages.add(pageCount - 2);
    pages.add(pageCount - 1);
  } else {
    pages.add(currentPage - 1);
    pages.add(currentPage);
    pages.add(currentPage + 1);
  }
  const ordered = [...pages].filter((page) => page >= 1 && page <= pageCount).sort((a, b) => a - b);
  const out = [];
  for (const page of ordered) {
    if (out.length && page - out[out.length - 1] > 1) out.push("...");
    out.push(page);
  }
  return out;
}

function renderPagination(pageCount) {
  if (pageCount <= 1) return;
  const nav = document.createElement("nav");
  nav.className = "pagination";
  nav.setAttribute("aria-label", "Paginacion");

  const makeButton = (label, page, extraClass = "") => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.className = `page-button ${extraClass}`.trim();
    button.disabled = page === currentPage || page < 1 || page > pageCount;
    button.addEventListener("click", () => {
      currentPage = page;
      saveNumber("arr_page", currentPage);
      render(lastResults);
      resultsBox.scrollIntoView({ block: "start", behavior: "smooth" });
    });
    return button;
  };

  nav.appendChild(makeButton("Anterior", currentPage - 1, "page-wide"));
  for (const page of pageSequence(pageCount)) {
    if (page === "...") {
      const ellipsis = document.createElement("span");
      ellipsis.className = "page-ellipsis";
      ellipsis.textContent = "...";
      nav.appendChild(ellipsis);
      continue;
    }
    nav.appendChild(makeButton(String(page), page, page === currentPage ? "is-current" : ""));
  }
  nav.appendChild(makeButton("Siguiente", currentPage + 1, "page-wide"));
  resultsBox.appendChild(nav);
}

function resetPageAndRender(items) {
  currentPage = 1;
  saveNumber("arr_page", currentPage);
  render(items);
}

function render(items) {
  resultsBox.textContent = "";
  const visibleItems = filteredAndSorted(items);
  if (!visibleItems.length) {
    resultsBox.innerHTML = '<div class="empty">Sin resultados</div>';
    status("Sin resultados");
    return;
  }
  const pageCount = Math.max(1, Math.ceil(visibleItems.length / PAGE_SIZE));
  currentPage = Math.max(1, Math.min(currentPage, pageCount));
  const start = (currentPage - 1) * PAGE_SIZE;
  const pageItems = visibleItems.slice(start, start + PAGE_SIZE);
  status(`${visibleItems.length} resultados`);
  for (const item of pageItems) {
    const row = document.createElement("article");
    row.className = "item";
    row.tabIndex = 0;
    row.role = "button";
    row.dataset.busy = "0";
    row.setAttribute("aria-label", `Enviar ${item.title}`);

    const info = document.createElement("div");
    info.className = "item-main";
    const title = document.createElement("div");
    title.className = "title";
    title.textContent = item.title;
    const meta = document.createElement("div");
    meta.className = "meta";
    for (const part of metaText(item)) {
      const span = document.createElement("span");
      span.textContent = part;
      meta.appendChild(span);
    }

    info.append(title, meta);
    row.addEventListener("click", () => send(item, row));
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        send(item, row);
      }
    });
    row.append(info);
    resultsBox.appendChild(row);
    restoreSendState(item, row);
  }
  renderPagination(pageCount);
}

function filteredAndSorted(items) {
  const activeTrackers = effectiveTrackerIds();
  const filtered = activeTrackers.length
    ? items.filter((item) => activeTrackers.includes(item.tracker_id))
    : trackerMode === "custom"
      ? []
      : [...items];
  for (let index = filtered.length - 1; index >= 0; index -= 1) {
    if (numberValue(filtered[index].seeders) < minSeeders || numberValue(filtered[index].peers) < minPeers) {
      filtered.splice(index, 1);
    }
  }
  filtered.sort((a, b) => {
    if (sortMode.value === "size_desc") return numberValue(b.size) - numberValue(a.size);
    if (sortMode.value === "size_asc") return numberValue(a.size) - numberValue(b.size);
    if (sortMode.value === "peers_desc") return numberValue(b.peers) - numberValue(a.peers);
    if (sortMode.value === "tracker_asc") return String(a.tracker || "").localeCompare(String(b.tracker || ""));
    if (sortMode.value === "tracker_desc") return String(b.tracker || "").localeCompare(String(a.tracker || ""));
    return numberValue(b.seeders) - numberValue(a.seeders);
  });
  return filtered;
}

trackerButton.addEventListener("click", () => openTrackerPanel(trackerPanel.hidden));
trackerSearch.addEventListener("input", renderTrackerList);

trackerAll.addEventListener("click", () => {
  trackerMode = "all";
  selectedTrackers.clear();
  saveTrackers();
  renderTrackerList();
  updateTrackerButton();
});

trackerNone.addEventListener("click", () => {
  trackerMode = "custom";
  selectedTrackers.clear();
  saveTrackers();
  renderTrackerList();
  updateTrackerButton();
});

document.addEventListener("click", (event) => {
  if (!event.target.closest(".tracker-picker")) openTrackerPanel(false);
});

sortMode.addEventListener("change", () => {
  localStorage.setItem("arr_sort", sortMode.value);
  updateSortButtons();
  resetPageAndRender(lastResults);
});

categoryInput.addEventListener("change", () => {
  localStorage.setItem("arr_category", categoryInput.value);
  updateCategoryButton();
});

queryInput.addEventListener("input", () => localStorage.setItem("arr_query", queryInput.value));
clearSearchBtn.addEventListener("click", clearSearchMemory);
categoryButton.addEventListener("click", cycleCategory);

sortGbButton.addEventListener("click", () => {
  sortMode.value = sortMode.value === "size_desc" ? "size_asc" : "size_desc";
  localStorage.setItem("arr_sort", sortMode.value);
  updateSortButtons();
  resetPageAndRender(lastResults);
});

sortAzButton.addEventListener("click", () => {
  sortMode.value = sortMode.value === "tracker_asc" ? "tracker_desc" : "tracker_asc";
  localStorage.setItem("arr_sort", sortMode.value);
  updateSortButtons();
  resetPageAndRender(lastResults);
});

seedMinButton.addEventListener("click", () => {
  minSeeders = askMinimum("Semillas", minSeeders);
  saveNumber("arr_min_seeders", minSeeders);
  updateLimitButtons();
  resetPageAndRender(lastResults);
});

peerMinButton.addEventListener("click", () => {
  minPeers = askMinimum("Pares", minPeers);
  saveNumber("arr_min_peers", minPeers);
  updateLimitButtons();
  resetPageAndRender(lastResults);
});

settingsToggle.addEventListener("click", () => showSettings(settingsView.hidden));
cancelSettingsBtn.addEventListener("click", () => {
  renderSettings(settings);
  showSettings(false);
});
saveSettingsBtn.addEventListener("click", saveSettings);
saveSettingsQbitBtn.addEventListener("click", saveSettings);
testClassifyBtn.addEventListener("click", classifyProbe);
classifyTitle.addEventListener("keydown", (event) => {
  if (event.key === "Enter") classifyProbe();
});
testRdtBtn.addEventListener("click", () => testEndpoint("/api/test/rdt", "RD/RDT"));
testQbitBtn.addEventListener("click", () => testEndpoint("/api/test/qbit", "qB"));

document.querySelectorAll(".settings-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".settings-tab").forEach((item) => item.classList.toggle("is-active", item === tab));
    document.querySelectorAll(".settings-panel").forEach((panel) => panel.classList.toggle("is-active", panel.dataset.panel === tab.dataset.tab));
    localStorage.setItem("arr_settings_tab", tab.dataset.tab);
  });
});

document.querySelectorAll("[data-reset]").forEach((button) => {
  button.addEventListener("click", () => resetSettings(button.dataset.reset));
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const q = queryInput.value.trim();
  if (!q) return;
  if (trackerMode === "custom" && !selectedTrackers.size) {
    lastResults = [];
    resetPageAndRender([]);
    status("Elige al menos un tracker");
    return;
  }

  localStorage.setItem("arr_category", categoryInput.value);
  localStorage.setItem("arr_sort", sortMode.value);
  localStorage.setItem("arr_query", q);
  saveTrackers();
  startSearchJob(q);
});

const savedSettingsTab = localStorage.getItem("arr_settings_tab") || "rdt";
document.querySelectorAll(".settings-tab").forEach((tab) => {
  tab.classList.toggle("is-active", tab.dataset.tab === savedSettingsTab);
});
document.querySelectorAll(".settings-panel").forEach((panel) => {
  panel.classList.toggle("is-active", panel.dataset.panel === savedSettingsTab);
});

let scrollSaveTimer = 0;
window.addEventListener("scroll", () => {
  clearTimeout(scrollSaveTimer);
  scrollSaveTimer = setTimeout(saveViewState, 150);
}, { passive: true });
window.addEventListener("pagehide", saveViewState);

loadSettings();
loadIndexers();
updateCategoryButton();
updateSortButtons();
updateLimitButtons();
showSettings(localStorage.getItem("arr_view") === "settings");
if (activeSearchJobId) pollSearchJob(activeSearchJobId);
