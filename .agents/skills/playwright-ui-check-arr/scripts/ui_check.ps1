param(
  [string]$PanelUrl = "http://192.168.1.159:5830",
  [int]$TimeoutMs = 10000,
  [ValidateSet("auto", "chrome", "msedge")]
  [string]$Browser = "auto",
  [switch]$KeepBrowserOpen
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..\..")).Path
Set-Location $Root

$stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
$stamp = Get-Date -Format "yyyyMMdd_HHmmss_fff"
$artifactDir = Join-Path $Root "_codex_runtime\artifacts\ui-check\$stamp"
$runtimeDir = Join-Path $Root "_codex_runtime\tmp\playwright-ui-check-arr"
New-Item -ItemType Directory -Force -Path $artifactDir, $runtimeDir | Out-Null

$node = Get-Command node.exe -ErrorAction SilentlyContinue
if (-not $node) {
  throw "ARR_UI_CHECK_NODE_MISSING"
}

$npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $npm) {
  throw "ARR_UI_CHECK_NPM_CMD_MISSING"
}

$env:NODE_PATH = Join-Path $runtimeDir "node_modules"
$packageJson = Join-Path $runtimeDir "package.json"
$playwrightPkg = Join-Path $runtimeDir "node_modules\playwright\package.json"

if (-not (Test-Path -LiteralPath $packageJson -PathType Leaf)) {
  Push-Location $runtimeDir
  try {
    & $npm.Source init -y | Out-Null
  } finally {
    Pop-Location
  }
}

if (-not (Test-Path -LiteralPath $playwrightPkg -PathType Leaf)) {
  Push-Location $runtimeDir
  try {
    & $npm.Source install playwright@1.55.0 --no-audit --no-fund
  } finally {
    Pop-Location
  }
}

$runner = Join-Path $artifactDir "ui_check_runner.js"
$script = @'
const fs = require("fs");
const path = require("path");
const { isDeepStrictEqual } = require("util");
const { chromium, devices } = require("playwright");

const panelUrl = process.argv[2];
const artifactDir = process.argv[3];
const timeoutMs = Number(process.argv[4] || "10000");
const keepBrowserOpen = process.argv[5] === "1";
const browserMode = process.argv[6] || "auto";
const startedAt = new Date().toISOString();
const baseUrl = new URL(panelUrl);
baseUrl.hash = "";
const globalTimeoutMs = Math.max(120000, timeoutMs * 18);
let globalTimeoutReached = false;
const globalWatchdog = setTimeout(() => {
  globalTimeoutReached = true;
  console.error("ARR_UI_CHECK_GLOBAL_TIMEOUT_REACHED");
}, globalTimeoutMs);
globalWatchdog.unref();

function writeJson(name, payload) {
  fs.writeFileSync(path.join(artifactDir, name), JSON.stringify(payload, null, 2), "utf8");
}

function textPreview(value, limit = 1200) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > limit ? text.slice(0, limit) + "..." : text;
}

function errorPayload(error) {
  return {
    name: error && error.name ? error.name : "Error",
    message: textPreview(error && error.message ? error.message : error, 1600),
    code: error && error.code ? error.code : null,
  };
}

function fail(code, details) {
  const error = new Error(code + (details ? " " + details : ""));
  error.code = code;
  throw error;
}

function check(condition, code, details = "") {
  if (!condition) fail(code, details);
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function hrefFor(hash = "") {
  const target = new URL(baseUrl.href);
  target.hash = String(hash || "").replace(/^#/, "");
  return target.href;
}

function pathOf(rawUrl) {
  try {
    return new URL(rawUrl).pathname;
  } catch (_error) {
    return "";
  }
}

function createObservationBucket(label) {
  return {
    label,
    console_events: [],
    page_errors: [],
    network: [],
    downloads: [],
    forbidden_actions: [],
    expected_cas_conflict: false,
  };
}

function attachObservers(page, bucket) {
  page.on("console", message => {
    const text = textPreview(message.text(), 1600);
    bucket.console_events.push({
      profile: bucket.label,
      type: message.type(),
      text,
      expected: Boolean(
        bucket.expected_cas_conflict
        && message.type() === "error"
        && /(?:409|series-rules|conflict)/i.test(text)
      ),
      location: message.location(),
    });
  });
  page.on("pageerror", error => {
    bucket.page_errors.push({
      profile: bucket.label,
      name: error.name,
      message: textPreview(error.message, 1600),
    });
  });
  page.on("request", request => {
    const pathname = pathOf(request.url());
    const method = request.method();
    if (
      (pathname === "/api/codex-diagnostic" && method === "POST")
      || (pathname === "/api/codex-diagnostic" && method === "GET")
    ) {
      bucket.forbidden_actions.push({
        profile: bucket.label,
        kind: "codex_diagnostic_action",
        method,
        url: request.url(),
      });
    }
  });
  page.on("requestfailed", request => {
    const pathname = pathOf(request.url());
    if (pathname === "/favicon.ico") return;
    const failure = request.failure();
    bucket.network.push({
      profile: bucket.label,
      kind: "requestfailed",
      method: request.method(),
      resourceType: request.resourceType(),
      url: request.url(),
      failure: failure ? failure.errorText : "",
      expected: false,
    });
  });
  page.on("response", response => {
    const status = response.status();
    if (status < 400) return;
    const request = response.request();
    const pathname = pathOf(response.url());
    if (pathname === "/favicon.ico") return;
    const expected = Boolean(
      bucket.expected_cas_conflict
      && pathname === "/api/series-rules"
      && request.method() === "POST"
      && status === 409
    );
    bucket.network.push({
      profile: bucket.label,
      kind: "bad_response",
      status,
      method: request.method(),
      resourceType: request.resourceType(),
      url: response.url(),
      expected,
    });
  });
  page.on("download", download => {
    bucket.downloads.push({
      profile: bucket.label,
      suggested_filename: download.suggestedFilename(),
      url: download.url(),
    });
  });
}

function blockingNetworkEvents(events) {
  return events.filter(event => event.expected !== true);
}

function blockingConsoleErrors(events) {
  return events.filter(event => event.type === "error" && event.expected !== true);
}

function bucketSummary(bucket) {
  return {
    console_error_count: blockingConsoleErrors(bucket.console_events).length,
    page_error_count: bucket.page_errors.length,
    blocking_network_count: blockingNetworkEvents(bucket.network).length,
    expected_network_count: bucket.network.filter(event => event.expected === true).length,
    download_count: bucket.downloads.length,
    forbidden_action_count: bucket.forbidden_actions.length,
  };
}

function bucketOk(bucket) {
  const summary = bucketSummary(bucket);
  return summary.console_error_count === 0
    && summary.page_error_count === 0
    && summary.blocking_network_count === 0
    && summary.download_count === 0
    && summary.forbidden_action_count === 0;
}

function profileDefinitions() {
  const mobileDevice = devices["Pixel 5"] || {};
  return [
    {
      name: "desktop",
      screenshot: "desktop_screenshot.png",
      context: {
        viewport: { width: 1440, height: 1000 },
        ignoreHTTPSErrors: true,
      },
      expected: {
        width: 1440,
        height: 1000,
        device_scale_factor: 1,
        touch: false,
      },
    },
    {
      name: "mobile",
      screenshot: "mobile_screenshot.png",
      context: {
        ...mobileDevice,
        viewport: { width: 390, height: 844 },
        isMobile: true,
        hasTouch: true,
        deviceScaleFactor: 2,
        ignoreHTTPSErrors: true,
      },
      expected: {
        width: 390,
        height: 844,
        device_scale_factor: 2,
        touch: true,
      },
    },
  ];
}

const identityProfiles = [
  ["common", "comun"],
  ["movies", "peliculas"],
  ["tv", "series"],
];
const identitySections = ["parser", "resolver"];
const cleaningSections = [
  ["entrada", "entrada.extensiones_video"],
  ["video", "video.pistas_exactas"],
  ["audio", "audio.idiomas_aceptados"],
  ["subtitulos", "subtitulos.idiomas_aceptados"],
  ["limpieza", "limpieza.crear_capitulos"],
];

function canonicalRoutes() {
  const routes = [];
  for (const [profile, slug] of identityProfiles) {
    for (const section of identitySections) {
      routes.push({
        hash: "#identidad/" + slug + "/" + section,
        view: "identidad",
        title: "Identidad ARR",
        selector: ".identity-shell[data-identity-profile=\"" + profile + "\"][data-identity-section=\"" + section + "\"]",
      });
    }
  }
  for (const view of ["limpieza-peliculas", "limpieza-series"]) {
    for (const [section, witness] of cleaningSections) {
      routes.push({
        hash: "#" + view + "/" + section,
        view,
        title: view === "limpieza-peliculas" ? "Limpieza películas" : "Limpieza series",
        selector: ".rule-profile-shell[data-rule-view=\"" + view + "\"] [data-rule-path=\"" + witness + "\"]",
      });
    }
  }
  routes.push({
    hash: "#ajustes/trailers",
    view: "ajustes",
    title: "Ajustes",
    selector: ".rule-profile-shell[data-rule-view=\"ajustes\"][data-rule-source=\"trailers\"] [data-rule-path=\"trailers.extensiones_video\"]",
  });
  routes.push({
    hash: "#ajustes/vigilante-peliculas",
    view: "ajustes",
    title: "Ajustes",
    selector: ".rule-profile-shell[data-rule-view=\"ajustes\"][data-rule-source=\"watcherMovies\"] [data-rule-path=\"ignored_suffixes\"]",
  });
  routes.push({
    hash: "#ajustes/vigilante-series",
    view: "ajustes",
    title: "Ajustes",
    selector: ".rule-profile-shell[data-rule-view=\"ajustes\"][data-rule-source=\"watcherTv\"] [data-rule-path=\"ignored_suffixes\"]",
  });
  routes.push({
    hash: "#motor",
    view: "motor",
    title: "Estado del motor",
    selector: "#app .grid .card",
  });
  routes.push({
    hash: "#historial",
    view: "historial",
    title: "Historial",
    selector: "#app .panel",
  });
  routes.push({
    hash: "#revision",
    view: "revision",
    title: "Revisión",
    selector: "#app .auxiliary-profile-tabs",
  });
  routes.push({
    hash: "#informes",
    view: "informes",
    title: "Informes",
    selector: "#app .auxiliary-profile-tabs",
  });
  return routes;
}

const canonical = canonicalRoutes();
const canonicalByHash = new Map(canonical.map(route => [route.hash, route]));

function legacyRoutes() {
  return [
    {
      source: "#limpieza-arr",
      expected: "#identidad/comun/resolver",
      storage: { "arr-identity-section-common": "resolver" },
    },
    { source: "#limpieza-arr/parser", expected: "#identidad/comun/parser" },
    { source: "#limpieza-arr/resolver", expected: "#identidad/comun/resolver" },
    {
      source: "#reglas",
      expected: "#limpieza-peliculas/audio",
      storage: { "arr-media-panel-rule-section": "audio" },
    },
    { source: "#reglas/entrada", expected: "#limpieza-peliculas/entrada" },
    { source: "#reglas/video", expected: "#limpieza-peliculas/video" },
    { source: "#reglas/audio", expected: "#limpieza-peliculas/audio" },
    { source: "#reglas/subtitulos", expected: "#limpieza-peliculas/subtitulos" },
    { source: "#reglas/limpieza", expected: "#limpieza-peliculas/limpieza" },
    { source: "#reglas/trailers", expected: "#ajustes/trailers" },
    {
      source: "#reglas/vigilante",
      expected: "#ajustes/vigilante-peliculas",
      storage: { "arr-media-panel-ajustes-vigilante": "movies" },
    },
    {
      source: "#reglas/vigilantes",
      expected: "#ajustes/vigilante-series",
      storage: { "arr-media-panel-ajustes-vigilante": "tv" },
    },
    {
      source: "#ajustes/vigilantes",
      expected: "#ajustes/vigilante-peliculas",
      storage: { "arr-media-panel-ajustes-vigilante": "movies" },
    },
    {
      source: "#identidad",
      expected: "#identidad/series/resolver",
      storage: {
        "arr-identity-profile": "tv",
        "arr-identity-section-tv": "resolver",
      },
    },
    {
      source: "#identidad/peliculas",
      expected: "#identidad/peliculas/resolver",
      storage: { "arr-identity-section-movies": "resolver" },
    },
    {
      source: "#limpieza-peliculas",
      expected: "#limpieza-peliculas/audio",
      storage: { "arr-media-panel-section-movies": "audio" },
    },
    {
      source: "#limpieza-series",
      expected: "#limpieza-series/subtitulos",
      storage: { "arr-media-panel-section-series": "subtitulos" },
    },
    {
      source: "#ajustes",
      expected: "#ajustes/vigilante-series",
      storage: {
        "arr-media-panel-section-settings": "vigilantes",
        "arr-media-panel-ajustes-vigilante": "tv",
      },
    },
  ];
}

async function applyStorage(page, values) {
  if (!values || Object.keys(values).length === 0) return;
  await page.evaluate(entries => {
    for (const [key, value] of Object.entries(entries)) {
      localStorage.setItem(key, value);
    }
  }, values);
}

async function waitForRoute(page, route) {
  await page.waitForFunction(expected => {
    const active = document.querySelector(".tabs button[data-view=\"" + expected.view + "\"].active");
    const title = document.getElementById("title");
    return location.hash === expected.hash
      && Boolean(active)
      && Boolean(title)
      && title.textContent.trim() === expected.title;
  }, route, { timeout: timeoutMs });
  await page.locator(route.selector).first().waitFor({ state: "visible", timeout: timeoutMs });
}

async function directOpen(page, sourceHash, expectedRoute) {
  await page.goto("about:blank");
  const response = await page.goto(hrefFor(sourceHash), {
    waitUntil: "domcontentloaded",
    timeout: timeoutMs,
  });
  const status = response ? response.status() : 0;
  check(status >= 200 && status < 400, "ARR_UI_ROUTE_HTTP", sourceHash + " status=" + status);
  await waitForRoute(page, expectedRoute);
  return status;
}

async function inspectLayout(page, label) {
  return page.evaluate(checkLabel => {
    const doc = document.documentElement;
    const body = document.body;
    const clientWidth = doc.clientWidth || window.innerWidth;
    const scrollWidth = Math.max(doc.scrollWidth || 0, body ? body.scrollWidth || 0 : 0);
    const clientHeight = doc.clientHeight || window.innerHeight;
    const scrollHeight = Math.max(doc.scrollHeight || 0, body ? body.scrollHeight || 0 : 0);
    const overflowX = scrollWidth > clientWidth + 4;
    const offenders = overflowX
      ? [...document.querySelectorAll("body *")]
          .map(element => {
            const rect = element.getBoundingClientRect();
            return {
              tag: element.tagName.toLowerCase(),
              id: element.id || "",
              class_name: typeof element.className === "string" ? element.className.slice(0, 160) : "",
              left: Math.round(rect.left),
              right: Math.round(rect.right),
              width: Math.round(rect.width),
            };
          })
          .filter(item => item.right > clientWidth + 4 || item.left < -4)
          .slice(0, 12)
      : [];
    return {
      label: checkLabel,
      ok: !overflowX,
      viewport_width: window.innerWidth,
      viewport_height: window.innerHeight,
      device_scale_factor: window.devicePixelRatio,
      max_touch_points: navigator.maxTouchPoints || 0,
      coarse_pointer: window.matchMedia("(pointer: coarse)").matches,
      client_width: clientWidth,
      scroll_width: scrollWidth,
      client_height: clientHeight,
      scroll_height: scrollHeight,
      has_horizontal_overflow: overflowX,
      offenders,
    };
  }, label);
}

async function captureCapabilities(page, expected) {
  const actual = await page.evaluate(() => ({
    width: window.innerWidth,
    height: window.innerHeight,
    device_scale_factor: window.devicePixelRatio,
    max_touch_points: navigator.maxTouchPoints || 0,
    coarse_pointer: window.matchMedia("(pointer: coarse)").matches,
  }));
  const ok = actual.width === expected.width
    && actual.height === expected.height
    && Math.abs(actual.device_scale_factor - expected.device_scale_factor) < 0.01
    && (!expected.touch || actual.max_touch_points > 0);
  return { ok, expected, actual };
}

async function jsonRequest(request, method, pathname, payload = undefined) {
  const options = { timeout: timeoutMs, failOnStatusCode: false };
  if (payload !== undefined) options.data = payload;
  const response = method === "POST"
    ? await request.post(new URL(pathname, baseUrl).href, options)
    : await request.get(new URL(pathname, baseUrl).href, options);
  const raw = await response.text();
  let body = null;
  try {
    body = raw ? JSON.parse(raw) : {};
  } catch (_error) {
    fail("ARR_UI_INVALID_JSON", pathname + " status=" + response.status());
  }
  return { status: response.status(), ok: response.ok(), body };
}

async function endpointChecks(request) {
  const checks = [];
  for (const [name, pathname] of [
    ["health", "/health"],
    ["diagnostics", "/api/codex-diagnostics"],
  ]) {
    try {
      const result = await jsonRequest(request, "GET", pathname);
      checks.push({
        name,
        path: pathname,
        ok: result.ok && result.status >= 200 && result.status < 300 && result.body && typeof result.body === "object",
        status: result.status,
      });
    } catch (error) {
      checks.push({ name, path: pathname, ok: false, error: errorPayload(error) });
    }
  }
  return checks;
}

async function runStep(collection, label, action) {
  const started = Date.now();
  try {
    check(!globalTimeoutReached, "ARR_UI_CHECK_GLOBAL_TIMEOUT", label);
    const detail = await action();
    collection.push({
      label,
      ok: true,
      duration_ms: Date.now() - started,
      ...(detail || {}),
    });
    return detail;
  } catch (error) {
    collection.push({
      label,
      ok: false,
      duration_ms: Date.now() - started,
      error: errorPayload(error),
    });
    return null;
  }
}

async function clickMainView(page, view) {
  await page.locator(".tabs button[data-view=\"" + view + "\"]").click();
  await page.waitForFunction(expectedView => {
    const button = document.querySelector(".tabs button[data-view=\"" + expectedView + "\"]");
    if (!button || !button.classList.contains("active")) return false;
    const hash = location.hash;
    const patterns = {
      identidad: /^#identidad\/(comun|peliculas|series)\/(parser|resolver)$/,
      "limpieza-peliculas": /^#limpieza-peliculas\/(entrada|video|audio|subtitulos|limpieza)$/,
      "limpieza-series": /^#limpieza-series\/(entrada|video|audio|subtitulos|limpieza)$/,
      ajustes: /^#ajustes\/(trailers|vigilante-peliculas|vigilante-series)$/,
      motor: /^#motor$/,
      historial: /^#historial$/,
      revision: /^#revision$/,
      informes: /^#informes$/,
    };
    return patterns[expectedView].test(hash);
  }, view, { timeout: timeoutMs });
  const currentHash = await page.evaluate(() => location.hash);
  const route = canonicalByHash.get(currentHash);
  check(Boolean(route), "ARR_UI_MAIN_NON_CANONICAL", view + " hash=" + currentHash);
  await waitForRoute(page, route);
  return route;
}

async function verifyLayout(page, layouts, label) {
  const layout = await inspectLayout(page, label);
  layouts.push(layout);
  check(layout.ok, "ARR_UI_HORIZONTAL_OVERFLOW", label + " " + layout.scroll_width + ">" + layout.client_width);
  return layout;
}

async function runCanonicalRoutes(page, routeChecks, layouts) {
  for (const route of canonical) {
    await runStep(routeChecks, "canonical " + route.hash, async () => {
      const status = await directOpen(page, route.hash, route);
      const layout = await verifyLayout(page, layouts, "canonical " + route.hash);
      return {
        kind: "canonical",
        source_hash: route.hash,
        final_hash: await page.evaluate(() => location.hash),
        view: route.view,
        http_status: status,
        layout_ok: layout.ok,
      };
    });
  }
}

async function runLegacyRoutes(page, routeChecks, layouts) {
  for (const legacy of legacyRoutes()) {
    await runStep(routeChecks, "redirect " + legacy.source, async () => {
      await applyStorage(page, legacy.storage);
      const expectedRoute = canonicalByHash.get(legacy.expected);
      check(Boolean(expectedRoute), "ARR_UI_LEGACY_EXPECTATION_UNKNOWN", legacy.expected);
      const status = await directOpen(page, legacy.source, expectedRoute);
      const finalHash = await page.evaluate(() => location.hash);
      check(finalHash === legacy.expected, "ARR_UI_LEGACY_REDIRECT", legacy.source + " -> " + finalHash);
      const layout = await verifyLayout(page, layouts, "redirect " + legacy.source);
      return {
        kind: "redirect",
        source_hash: legacy.source,
        expected_hash: legacy.expected,
        final_hash: finalHash,
        http_status: status,
        layout_ok: layout.ok,
      };
    });
  }
}

async function runMainMenu(page, interactions, layouts) {
  const motorRoute = canonicalByHash.get("#motor");
  await runStep(interactions, "menu principal setup", async () => {
    await directOpen(page, motorRoute.hash, motorRoute);
    return { final_hash: motorRoute.hash };
  });
  for (const view of [
    "identidad",
    "limpieza-peliculas",
    "limpieza-series",
    "ajustes",
    "motor",
    "historial",
    "revision",
    "informes",
  ]) {
    await runStep(interactions, "menu principal " + view, async () => {
      const route = await clickMainView(page, view);
      const layout = await verifyLayout(page, layouts, "menu principal " + view);
      return { view, final_hash: route.hash, layout_ok: layout.ok };
    });
  }
}

async function runIdentityMenu(page, interactions, layouts) {
  const setupRoute = canonicalByHash.get("#identidad/comun/parser");
  await runStep(interactions, "identidad setup", async () => {
    await directOpen(page, setupRoute.hash, setupRoute);
    return { final_hash: setupRoute.hash };
  });
  for (const [profile, slug] of identityProfiles) {
    await runStep(interactions, "identidad perfil " + profile, async () => {
      await page.locator(".identity-profile-tabs button[data-identity-profile=\"" + profile + "\"]").click();
      await page.waitForFunction(expectedProfile => {
        const shell = document.querySelector(".identity-shell");
        return shell && shell.dataset.identityProfile === expectedProfile;
      }, profile, { timeout: timeoutMs });
      return { profile, final_hash: await page.evaluate(() => location.hash) };
    });
    for (const section of identitySections) {
      await runStep(interactions, "identidad " + profile + " " + section, async () => {
        await page.locator(".identity-subtabs button[data-identity-section=\"" + section + "\"]").click();
        const expected = canonicalByHash.get("#identidad/" + slug + "/" + section);
        await waitForRoute(page, expected);
        const layout = await verifyLayout(page, layouts, "identidad " + profile + " " + section);
        return { profile, section, final_hash: expected.hash, layout_ok: layout.ok };
      });
    }
  }
}

async function runCleaningMenus(page, interactions, layouts) {
  for (const view of ["limpieza-peliculas", "limpieza-series"]) {
    const setupRoute = canonicalByHash.get("#" + view + "/entrada");
    await runStep(interactions, view + " setup", async () => {
      await directOpen(page, setupRoute.hash, setupRoute);
      return { final_hash: setupRoute.hash };
    });
    for (const [section] of cleaningSections) {
      await runStep(interactions, view + " sección " + section, async () => {
        await page.locator("button[data-rule-section=\"" + section + "\"]").click();
        const expected = canonicalByHash.get("#" + view + "/" + section);
        await waitForRoute(page, expected);
        const active = await page.locator("button[data-rule-section=\"" + section + "\"]").getAttribute("class");
        check(String(active || "").split(/\s+/).includes("active"), "ARR_UI_RULE_MENU_NOT_ACTIVE", view + "/" + section);
        const layout = await verifyLayout(page, layouts, view + " sección " + section);
        return { view, section, final_hash: expected.hash, layout_ok: layout.ok };
      });
    }
  }
}

async function runSettingsMenus(page, interactions, layouts) {
  const setupRoute = canonicalByHash.get("#ajustes/trailers");
  await runStep(interactions, "ajustes setup", async () => {
    await directOpen(page, setupRoute.hash, setupRoute);
    return { final_hash: setupRoute.hash };
  });
  for (const section of ["trailers"]) {
    await runStep(interactions, "ajustes sección " + section, async () => {
      await page.locator("button[data-rule-section=\"" + section + "\"]").click();
      const expected = canonicalByHash.get("#ajustes/" + section);
      await waitForRoute(page, expected);
      const layout = await verifyLayout(page, layouts, "ajustes sección " + section);
      return { section, final_hash: expected.hash, layout_ok: layout.ok };
    });
  }
  await runStep(interactions, "ajustes sección vigilantes", async () => {
    await page.locator("button[data-rule-section=\"vigilantes\"]").click();
    const finalHash = await page.evaluate(() => location.hash);
    const expected = canonicalByHash.get(finalHash);
    check(
      Boolean(expected) && expected.view === "ajustes" && finalHash !== "#ajustes/trailers",
      "ARR_UI_WATCHER_ROUTE_INVALID",
      finalHash,
    );
    await waitForRoute(page, expected);
    const layout = await verifyLayout(page, layouts, "ajustes sección vigilantes");
    return { section: "vigilantes", final_hash: finalHash, layout_ok: layout.ok };
  });
  for (const watcherProfile of ["movies", "tv"]) {
    await runStep(interactions, "vigilante " + watcherProfile, async () => {
      await page.locator("button[data-watcher-profile=\"" + watcherProfile + "\"]").click();
      const source = watcherProfile === "tv" ? "watcherTv" : "watcherMovies";
      const expected = canonicalByHash.get(
        watcherProfile === "tv"
          ? "#ajustes/vigilante-series"
          : "#ajustes/vigilante-peliculas",
      );
      await waitForRoute(page, expected);
      await page.locator(".rule-profile-shell[data-rule-source=\"" + source + "\"] [data-rule-path=\"ignored_suffixes\"]").waitFor({
        state: "visible",
        timeout: timeoutMs,
      });
      const active = await page.locator("button[data-watcher-profile=\"" + watcherProfile + "\"]").getAttribute("class");
      check(String(active || "").split(/\s+/).includes("active"), "ARR_UI_WATCHER_NOT_ACTIVE", watcherProfile);
      const layout = await verifyLayout(page, layouts, "vigilante " + watcherProfile);
      return { watcher_profile: watcherProfile, source, final_hash: expected.hash, layout_ok: layout.ok };
    });
  }
}

async function runAuxiliaryMenus(page, interactions, layouts) {
  for (const view of ["revision", "informes"]) {
    const route = canonicalByHash.get("#" + view);
    await runStep(interactions, view + " setup", async () => {
      await directOpen(page, route.hash, route);
      return { final_hash: route.hash };
    });
    for (const profile of ["movies", "series"]) {
      await runStep(interactions, view + " perfil " + profile, async () => {
        await page.locator(".auxiliary-profile-tabs button[data-aux-profile=\"" + profile + "\"]").click();
        await page.waitForFunction(expected => {
          const button = document.querySelector(".auxiliary-profile-tabs button[data-aux-profile=\"" + expected + "\"]");
          const app = document.getElementById("app");
          return button
            && button.classList.contains("active")
            && app
            && !/^Cargando\b/.test(app.textContent.trim());
        }, profile, { timeout: timeoutMs });
        const layout = await verifyLayout(page, layouts, view + " perfil " + profile);
        return { view, profile, final_hash: route.hash, layout_ok: layout.ok };
      });
    }
  }
}

async function runPersistence(page, checks, layouts) {
  const cases = [
    {
      label: "identidad perfil y sección",
      route: canonicalByHash.get("#identidad/peliculas/resolver"),
    },
    {
      label: "limpieza películas sección",
      route: canonicalByHash.get("#limpieza-peliculas/audio"),
    },
    {
      label: "limpieza series sección",
      route: canonicalByHash.get("#limpieza-series/subtitulos"),
    },
    {
      label: "ajustes vigilante series",
      route: canonicalByHash.get("#ajustes/vigilante-series"),
      prepare: async () => {
        await page.locator("button[data-watcher-profile=\"tv\"]").click();
        await page.locator(".rule-profile-shell[data-rule-source=\"watcherTv\"]").waitFor({
          state: "visible",
          timeout: timeoutMs,
        });
      },
      verify: async () => {
        const active = page.locator("button[data-watcher-profile=\"tv\"].active");
        await active.waitFor({ state: "visible", timeout: timeoutMs });
        return { watcher_profile: "tv" };
      },
    },
    {
      label: "revisión perfil series",
      route: canonicalByHash.get("#revision"),
      prepare: async () => {
        await page.locator("button[data-aux-profile=\"series\"]").click();
        await page.locator("button[data-aux-profile=\"series\"].active").waitFor({ state: "visible", timeout: timeoutMs });
      },
      verify: async () => {
        await page.locator("button[data-aux-profile=\"series\"].active").waitFor({ state: "visible", timeout: timeoutMs });
        return { auxiliary_profile: "series" };
      },
    },
    {
      label: "informes perfil series",
      route: canonicalByHash.get("#informes"),
      prepare: async () => {
        await page.locator("button[data-aux-profile=\"series\"]").click();
        await page.locator("button[data-aux-profile=\"series\"].active").waitFor({ state: "visible", timeout: timeoutMs });
      },
      verify: async () => {
        await page.locator("button[data-aux-profile=\"series\"].active").waitFor({ state: "visible", timeout: timeoutMs });
        return { auxiliary_profile: "series" };
      },
    },
  ];

  for (const item of cases) {
    await runStep(checks, "persistencia " + item.label, async () => {
      await directOpen(page, item.route.hash, item.route);
      if (item.prepare) await item.prepare();
      const response = await page.reload({ waitUntil: "domcontentloaded", timeout: timeoutMs });
      const status = response ? response.status() : 0;
      check(status >= 200 && status < 400, "ARR_UI_RELOAD_HTTP", item.label + " status=" + status);
      await waitForRoute(page, item.route);
      const extra = item.verify ? await item.verify() : {};
      const layout = await verifyLayout(page, layouts, "persistencia " + item.label);
      return {
        expected_hash: item.route.hash,
        final_hash: await page.evaluate(() => location.hash),
        reload_status: status,
        layout_ok: layout.ok,
        ...extra,
      };
    });
  }

  await runStep(checks, "persistencia ruta sin hash", async () => {
    const expected = canonicalByHash.get("#informes");
    await page.goto("about:blank");
    const response = await page.goto(baseUrl.href, { waitUntil: "domcontentloaded", timeout: timeoutMs });
    const status = response ? response.status() : 0;
    check(status >= 200 && status < 400, "ARR_UI_ROOT_HTTP", "status=" + status);
    await waitForRoute(page, expected);
    await page.locator("button[data-aux-profile=\"series\"].active").waitFor({ state: "visible", timeout: timeoutMs });
    const finalHash = await page.evaluate(() => location.hash);
    check(finalHash === expected.hash, "ARR_UI_STORED_ROUTE", finalHash);
    const layout = await verifyLayout(page, layouts, "persistencia ruta sin hash");
    return {
      expected_hash: expected.hash,
      final_hash: finalHash,
      http_status: status,
      auxiliary_profile: "series",
      layout_ok: layout.ok,
    };
  });
}

async function runProfile(browser, profile) {
  const bucket = createObservationBucket(profile.name);
  const routeChecks = [];
  const interactions = [];
  const persistence = [];
  const layouts = [];
  const profileStarted = Date.now();
  let context = null;
  let page = null;
  let screenshot = path.join(artifactDir, profile.screenshot);
  let capabilities = { ok: false };
  let endpoints = [];
  let bodyPreview = "";
  let fatalError = null;

  try {
    context = await browser.newContext(profile.context);
    page = await context.newPage();
    attachObservers(page, bucket);

    await runCanonicalRoutes(page, routeChecks, layouts);
    await runLegacyRoutes(page, routeChecks, layouts);
    await runMainMenu(page, interactions, layouts);
    await runIdentityMenu(page, interactions, layouts);
    await runCleaningMenus(page, interactions, layouts);
    await runSettingsMenus(page, interactions, layouts);
    await runAuxiliaryMenus(page, interactions, layouts);
    await runPersistence(page, persistence, layouts);

    const screenshotRoute = canonicalByHash.get("#ajustes/vigilante-series");
    await directOpen(page, screenshotRoute.hash, screenshotRoute);
    await page.locator("button[data-watcher-profile=\"tv\"]").click();
    await page.locator(".rule-profile-shell[data-rule-source=\"watcherTv\"]").waitFor({
      state: "visible",
      timeout: timeoutMs,
    });
    capabilities = await captureCapabilities(page, profile.expected);
    bodyPreview = await page.locator("body").innerText({ timeout: timeoutMs }).then(text => textPreview(text, 900));
    await page.screenshot({ path: screenshot, fullPage: true });
    endpoints = await endpointChecks(context.request);
  } catch (error) {
    fatalError = errorPayload(error);
    if (page) {
      await page.screenshot({ path: screenshot, fullPage: true }).catch(() => {});
    }
  } finally {
    if (context && !keepBrowserOpen) {
      await context.close().catch(() => {});
    }
  }

  const observation = bucketSummary(bucket);
  const canonicalChecks = routeChecks.filter(item => item.kind === "canonical");
  const redirectChecks = routeChecks.filter(item => item.kind === "redirect");
  const expectedCanonical = canonical.length;
  const expectedRedirects = legacyRoutes().length;
  const stepCollections = [...routeChecks, ...interactions, ...persistence];
  const ok = fatalError === null
    && canonicalChecks.length === expectedCanonical
    && redirectChecks.length === expectedRedirects
    && stepCollections.every(item => item.ok === true)
    && layouts.length > 0
    && layouts.every(item => item.ok === true)
    && capabilities.ok === true
    && endpoints.length === 2
    && endpoints.every(item => item.ok === true)
    && bodyPreview.length > 0
    && bucketOk(bucket);

  return {
    ok,
    profile: profile.name,
    duration_ms: Date.now() - profileStarted,
    expected_canonical_route_count: expectedCanonical,
    canonical_route_count: canonicalChecks.length,
    canonical_route_pass_count: canonicalChecks.filter(item => item.ok).length,
    expected_redirect_count: expectedRedirects,
    redirect_count: redirectChecks.length,
    redirect_pass_count: redirectChecks.filter(item => item.ok).length,
    interaction_count: interactions.length,
    interaction_pass_count: interactions.filter(item => item.ok).length,
    persistence_count: persistence.length,
    persistence_pass_count: persistence.filter(item => item.ok).length,
    layout_check_count: layouts.length,
    layout_pass_count: layouts.filter(item => item.ok).length,
    capabilities,
    endpoints,
    body_preview: bodyPreview,
    screenshot,
    fatal_error: fatalError,
    ...observation,
    details: {
      route_checks: routeChecks,
      interactions,
      persistence,
      layouts,
      observations: bucket,
    },
  };
}

function swappedExtensions(rules) {
  const mutated = clone(rules);
  const extensions = mutated
    && mutated.entrada
    && Array.isArray(mutated.entrada.extensiones_video)
    ? mutated.entrada.extensiones_video
    : null;
  check(extensions && extensions.length >= 2, "ARR_UI_SERIES_EXTENSIONS_TOO_SHORT");
  check(extensions[0] !== extensions[1], "ARR_UI_SERIES_EXTENSIONS_NOT_DISTINCT");
  const first = extensions[0];
  extensions[0] = extensions[1];
  extensions[1] = first;
  return mutated;
}

async function runDraftIsolation(browser) {
  const result = {
    ok: false,
    attempted: false,
    server_unchanged: false,
    series_draft_preserved: false,
    movies_draft_clean: false,
    draft_reverted: false,
  };
  const bucket = createObservationBucket("draft-isolation");
  let context = null;
  let error = null;
  try {
    context = await browser.newContext(profileDefinitions()[0].context);
    const page = await context.newPage();
    attachObservers(page, bucket);
    const seriesBefore = await jsonRequest(context.request, "GET", "/api/series-rules");
    const moviesBefore = await jsonRequest(context.request, "GET", "/api/movie-rules");
    check(seriesBefore.ok && moviesBefore.ok, "ARR_UI_DRAFT_BASELINE_HTTP");
    check(seriesBefore.body.connected === true && seriesBefore.body.editable === true, "ARR_UI_SERIES_NOT_EDITABLE");
    const mutated = swappedExtensions(seriesBefore.body.rules);
    const originalText = seriesBefore.body.rules.entrada.extensiones_video.join("\n");
    const mutatedText = mutated.entrada.extensiones_video.join("\n");

    const motorRoute = canonicalByHash.get("#motor");
    await directOpen(page, motorRoute.hash, motorRoute);
    await applyStorage(page, {
      "arr-media-panel-section-movies": "entrada",
      "arr-media-panel-section-series": "entrada",
    });
    const moviesRoute = canonicalByHash.get("#limpieza-peliculas/entrada");
    await directOpen(page, moviesRoute.hash, moviesRoute);
    result.attempted = true;

    const moviesInput = page.locator("[data-rule-path=\"entrada.extensiones_video\"]");
    check((await moviesInput.inputValue()) === moviesBefore.body.rules.entrada.extensiones_video.join("\n"), "ARR_UI_MOVIES_BASELINE_DRAFT");
    await clickMainView(page, "limpieza-series");
    const seriesInput = page.locator("[data-rule-path=\"entrada.extensiones_video\"]");
    check((await seriesInput.inputValue()) === originalText, "ARR_UI_SERIES_BASELINE_DRAFT");
    await seriesInput.fill(mutatedText);
    await page.locator("#save-rules-profile:not([disabled])").waitFor({ state: "visible", timeout: timeoutMs });

    await clickMainView(page, "limpieza-peliculas");
    result.movies_draft_clean = (await moviesInput.inputValue()) === moviesBefore.body.rules.entrada.extensiones_video.join("\n")
      && await page.locator("#save-rules-profile").isDisabled();
    check(result.movies_draft_clean, "ARR_UI_MOVIES_DRAFT_CONTAMINATED");

    await clickMainView(page, "limpieza-series");
    result.series_draft_preserved = (await seriesInput.inputValue()) === mutatedText
      && !(await page.locator("#save-rules-profile").isDisabled());
    check(result.series_draft_preserved, "ARR_UI_SERIES_DRAFT_NOT_PRESERVED");
    await seriesInput.fill(originalText);
    await page.waitForFunction(() => {
      const save = document.getElementById("save-rules-profile");
      return save && save.disabled;
    }, null, { timeout: timeoutMs });
    result.draft_reverted = (await seriesInput.inputValue()) === originalText
      && await page.locator("#save-rules-profile").isDisabled();
    check(result.draft_reverted, "ARR_UI_SERIES_DRAFT_NOT_REVERTED");

    const seriesAfter = await jsonRequest(context.request, "GET", "/api/series-rules");
    const moviesAfter = await jsonRequest(context.request, "GET", "/api/movie-rules");
    result.server_unchanged = seriesAfter.body.fingerprint === seriesBefore.body.fingerprint
      && moviesAfter.body.fingerprint === moviesBefore.body.fingerprint
      && isDeepStrictEqual(seriesAfter.body.rules, seriesBefore.body.rules)
      && isDeepStrictEqual(moviesAfter.body.rules, moviesBefore.body.rules);
    check(result.server_unchanged, "ARR_UI_DRAFT_TOUCHED_SERVER");
    await page.screenshot({ path: path.join(artifactDir, "draft_isolation.png"), fullPage: true });
  } catch (caught) {
    error = errorPayload(caught);
  } finally {
    if (context && !keepBrowserOpen) {
      await context.close().catch(() => {});
    }
  }
  result.observation = bucketSummary(bucket);
  result.error = error;
  result.ok = error === null
    && result.attempted
    && result.server_unchanged
    && result.series_draft_preserved
    && result.movies_draft_clean
    && result.draft_reverted
    && bucketOk(bucket);
  result.events = bucket;
  return result;
}

async function restoreSeriesExactly(request, originalSeries, mutationRules, mutationFingerprint) {
  const restoration = {
    attempted: false,
    post_status: null,
    safe_restore: false,
    surgical_cleanup: false,
    exact_rules_restored: false,
    exact_fingerprint_restored: false,
    final_fingerprint: null,
    error: null,
  };
  try {
    let current = await jsonRequest(request, "GET", "/api/series-rules");
    check(current.ok, "ARR_UI_SERIES_RESTORE_GET", "status=" + current.status);
    const alreadyOriginal = current.body.fingerprint === originalSeries.fingerprint
      && isDeepStrictEqual(current.body.rules, originalSeries.rules);
    if (!alreadyOriginal) {
      restoration.attempted = true;
      const currentIsOurMutation = isDeepStrictEqual(current.body.rules, mutationRules)
        && (!mutationFingerprint || current.body.fingerprint === mutationFingerprint);
      if (currentIsOurMutation) {
        restoration.safe_restore = true;
        const restored = await jsonRequest(request, "POST", "/api/series-rules", {
          rules: originalSeries.rules,
          expected_fingerprint: current.body.fingerprint,
        });
        restoration.post_status = restored.status;
        check(restored.ok, "ARR_UI_SERIES_RESTORE_POST", "status=" + restored.status);
      } else {
        const currentExtensions = current.body
          && current.body.rules
          && current.body.rules.entrada
          && current.body.rules.entrada.extensiones_video;
        const mutationExtensions = mutationRules
          && mutationRules.entrada
          && mutationRules.entrada.extensiones_video;
        if (isDeepStrictEqual(currentExtensions, mutationExtensions)) {
          restoration.surgical_cleanup = true;
          const cleanupRules = clone(current.body.rules);
          cleanupRules.entrada.extensiones_video = clone(originalSeries.rules.entrada.extensiones_video);
          const cleaned = await jsonRequest(request, "POST", "/api/series-rules", {
            rules: cleanupRules,
            expected_fingerprint: current.body.fingerprint,
          });
          restoration.post_status = cleaned.status;
          check(cleaned.ok, "ARR_UI_SERIES_SURGICAL_CLEANUP", "status=" + cleaned.status);
        } else {
          fail("ARR_UI_SERIES_RESTORE_UNSAFE", "current fingerprint=" + current.body.fingerprint);
        }
      }
      current = await jsonRequest(request, "GET", "/api/series-rules");
    }
    restoration.final_fingerprint = current.body.fingerprint || null;
    restoration.exact_rules_restored = isDeepStrictEqual(current.body.rules, originalSeries.rules);
    restoration.exact_fingerprint_restored = current.body.fingerprint === originalSeries.fingerprint;
    check(restoration.exact_rules_restored, "ARR_UI_SERIES_RULES_NOT_EXACTLY_RESTORED");
    check(restoration.exact_fingerprint_restored, "ARR_UI_SERIES_FINGERPRINT_NOT_RESTORED");
  } catch (error) {
    restoration.error = errorPayload(error);
  }
  restoration.ok = restoration.error === null
    && restoration.exact_rules_restored
    && restoration.exact_fingerprint_restored;
  return restoration;
}

async function runCasConflict(browser) {
  const result = {
    ok: false,
    attempted: false,
    precondition_active_equals_rules: false,
    mutation_kind: "swap_first_two_series_video_extensions",
    original_series_fingerprint: null,
    mutation_fingerprint: null,
    conflict_status: null,
    conflict_error: null,
    conflict_ui_message: "",
    stale_draft_preserved: false,
    movie_fingerprint_before: null,
    movie_fingerprint_after: null,
    movie_unchanged: false,
    restoration: { ok: false, attempted: false },
  };
  const bucketA = createObservationBucket("cas-tab-a");
  const bucketB = createObservationBucket("cas-tab-b");
  let context = null;
  let originalSeries = null;
  let originalMovies = null;
  let mutationRules = null;
  let operationalError = null;

  try {
    context = await browser.newContext(profileDefinitions()[0].context);
    const seriesResponse = await jsonRequest(context.request, "GET", "/api/series-rules");
    const moviesResponse = await jsonRequest(context.request, "GET", "/api/movie-rules");
    check(seriesResponse.ok && moviesResponse.ok, "ARR_UI_CAS_BASELINE_HTTP");
    originalSeries = clone(seriesResponse.body);
    originalMovies = clone(moviesResponse.body);
    result.original_series_fingerprint = originalSeries.fingerprint || null;
    result.movie_fingerprint_before = originalMovies.fingerprint || null;
    check(originalSeries.connected === true && originalSeries.editable === true, "ARR_UI_CAS_SERIES_NOT_EDITABLE");
    check(typeof originalSeries.fingerprint === "string" && originalSeries.fingerprint.length > 0, "ARR_UI_CAS_FINGERPRINT_MISSING");
    result.precondition_active_equals_rules = isDeepStrictEqual(originalSeries.active, originalSeries.rules);
    check(result.precondition_active_equals_rules, "ARR_UI_CAS_ACTIVE_NOT_FULL");
    mutationRules = swappedExtensions(originalSeries.rules);

    const pageA = await context.newPage();
    const pageB = await context.newPage();
    attachObservers(pageA, bucketA);
    attachObservers(pageB, bucketB);
    const seriesRoute = canonicalByHash.get("#limpieza-series/entrada");
    await Promise.all([
      directOpen(pageA, seriesRoute.hash, seriesRoute),
      directOpen(pageB, seriesRoute.hash, seriesRoute),
    ]);
    const originalText = originalSeries.rules.entrada.extensiones_video.join("\n");
    const mutationText = mutationRules.entrada.extensiones_video.join("\n");
    const inputA = pageA.locator("[data-rule-path=\"entrada.extensiones_video\"]");
    const inputB = pageB.locator("[data-rule-path=\"entrada.extensiones_video\"]");
    check((await inputA.inputValue()) === originalText, "ARR_UI_CAS_TAB_A_BASELINE");
    check((await inputB.inputValue()) === originalText, "ARR_UI_CAS_TAB_B_BASELINE");
    await inputA.fill(mutationText);
    await inputB.fill(mutationText);
    result.attempted = true;

    const saveAResponse = pageA.waitForResponse(response => (
      pathOf(response.url()) === "/api/series-rules"
      && response.request().method() === "POST"
    ), { timeout: timeoutMs });
    await pageA.locator("#save-rules-profile").click();
    const savedResponse = await saveAResponse;
    const savedBody = await savedResponse.json();
    check(savedResponse.status() === 200, "ARR_UI_CAS_FIRST_SAVE_STATUS", String(savedResponse.status()));
    check(savedBody && savedBody.ok !== false, "ARR_UI_CAS_FIRST_SAVE_BODY");
    check(typeof savedBody.fingerprint === "string" && savedBody.fingerprint !== originalSeries.fingerprint, "ARR_UI_CAS_MUTATION_FINGERPRINT");
    check(isDeepStrictEqual(savedBody.rules, mutationRules), "ARR_UI_CAS_MUTATION_RULES");
    result.mutation_fingerprint = savedBody.fingerprint;
    await pageA.waitForFunction(() => {
      const status = document.getElementById("rules-status");
      return status && /^Reglas guardadas\.\s+Motor de series\b/i.test(status.textContent);
    }, null, { timeout: timeoutMs });
    await pageA.screenshot({ path: path.join(artifactDir, "cas_tab_a_saved.png"), fullPage: true });

    bucketB.expected_cas_conflict = true;
    const conflictResponsePromise = pageB.waitForResponse(response => (
      pathOf(response.url()) === "/api/series-rules"
      && response.request().method() === "POST"
      && response.status() === 409
    ), { timeout: timeoutMs });
    await pageB.locator("#save-rules-profile").click();
    const conflictResponse = await conflictResponsePromise;
    const conflictBody = await conflictResponse.json();
    await pageB.waitForFunction(() => {
      const status = document.getElementById("rules-status");
      return status && /conflicto al guardar/i.test(status.textContent);
    }, null, { timeout: timeoutMs });
    bucketB.expected_cas_conflict = false;
    result.conflict_status = conflictResponse.status();
    result.conflict_error = conflictBody && conflictBody.error ? conflictBody.error : null;
    result.conflict_ui_message = textPreview(await pageB.locator("#rules-status").innerText(), 500);
    result.stale_draft_preserved = (await inputB.inputValue()) === mutationText
      && !(await pageB.locator("#save-rules-profile").isDisabled());
    check(result.conflict_status === 409, "ARR_UI_CAS_CONFLICT_STATUS");
    check(result.conflict_error === "fingerprint_conflict", "ARR_UI_CAS_CONFLICT_ERROR");
    check(result.stale_draft_preserved, "ARR_UI_CAS_STALE_DRAFT_LOST");
    await pageB.screenshot({ path: path.join(artifactDir, "cas_tab_b_conflict.png"), fullPage: true });
  } catch (error) {
    operationalError = errorPayload(error);
  } finally {
    bucketB.expected_cas_conflict = false;
    if (context && originalSeries && mutationRules) {
      result.restoration = await restoreSeriesExactly(
        context.request,
        originalSeries,
        mutationRules,
        result.mutation_fingerprint
      );
      try {
        const moviesAfter = await jsonRequest(context.request, "GET", "/api/movie-rules");
        result.movie_fingerprint_after = moviesAfter.body.fingerprint || null;
        result.movie_unchanged = moviesAfter.body.fingerprint === originalMovies.fingerprint
          && isDeepStrictEqual(moviesAfter.body.rules, originalMovies.rules);
      } catch (error) {
        if (!operationalError) operationalError = errorPayload(error);
      }
    }
    if (context && !keepBrowserOpen) {
      await context.close().catch(() => {});
    }
  }

  result.error = operationalError;
  result.observation = {
    tab_a: bucketSummary(bucketA),
    tab_b: bucketSummary(bucketB),
  };
  result.ok = operationalError === null
    && result.attempted
    && result.precondition_active_equals_rules
    && result.conflict_status === 409
    && result.conflict_error === "fingerprint_conflict"
    && result.stale_draft_preserved
    && result.restoration.ok === true
    && result.movie_unchanged
    && bucketOk(bucketA)
    && bucketOk(bucketB);
  result.events = { tab_a: bucketA, tab_b: bucketB };
  return result;
}

async function launchBrowser() {
  const launchOptions = { headless: !keepBrowserOpen };
  const candidates = browserMode === "auto" ? ["chrome", "msedge"] : [browserMode];
  const errors = [];
  for (const candidate of candidates) {
    try {
      return {
        browser: await chromium.launch({ ...launchOptions, channel: candidate }),
        mode: candidate,
      };
    } catch (error) {
      errors.push(candidate + ": " + error.message);
    }
  }
  fail("ARR_UI_CHECK_BROWSER_LAUNCH_FAILED", errors.join(" | "));
}

(async () => {
  let browser = null;
  let browserUsed = "";
  let launchError = null;
  const profiles = [];
  let draftIsolation = { ok: false, skipped: true, reason: "profiles_not_green" };
  let casSeries = { ok: false, skipped: true, reason: "profiles_not_green" };

  try {
    const launched = await launchBrowser();
    browser = launched.browser;
    browserUsed = launched.mode;
    for (const profile of profileDefinitions()) {
      profiles.push(await runProfile(browser, profile));
    }
    if (profiles.length === 2 && profiles.every(profile => profile.ok === true)) {
      draftIsolation = await runDraftIsolation(browser);
      if (draftIsolation.ok === true) {
        casSeries = await runCasConflict(browser);
      } else {
        casSeries = { ok: false, skipped: true, reason: "draft_isolation_not_green" };
      }
    }
  } catch (error) {
    launchError = errorPayload(error);
  } finally {
    if (browser && !keepBrowserOpen) {
      await browser.close().catch(() => {});
    }
  }

  const buckets = [];
  for (const profile of profiles) {
    if (profile.details && profile.details.observations) buckets.push(profile.details.observations);
  }
  if (draftIsolation.events) buckets.push(draftIsolation.events);
  if (casSeries.events) {
    buckets.push(casSeries.events.tab_a);
    buckets.push(casSeries.events.tab_b);
  }
  const consoleEvents = buckets.flatMap(bucket => bucket.console_events || []);
  const pageErrors = buckets.flatMap(bucket => bucket.page_errors || []);
  const network = buckets.flatMap(bucket => bucket.network || []);
  const downloads = buckets.flatMap(bucket => bucket.downloads || []);
  const forbiddenActions = buckets.flatMap(bucket => bucket.forbidden_actions || []);
  const blockingNetwork = blockingNetworkEvents(network);
  const consoleErrors = blockingConsoleErrors(consoleEvents);
  const summaryProfiles = profiles.map(profile => {
    const { details, ...summary } = profile;
    return summary;
  });
  const ok = launchError === null
    && summaryProfiles.length === 2
    && summaryProfiles.every(profile => profile.ok === true)
    && draftIsolation.ok === true
    && casSeries.ok === true
    && consoleErrors.length === 0
    && pageErrors.length === 0
    && blockingNetwork.length === 0
    && downloads.length === 0
    && forbiddenActions.length === 0;

  const summary = {
    ok,
    started_at: startedAt,
    finished_at: new Date().toISOString(),
    duration_ms: Date.now() - Date.parse(startedAt),
    global_timeout_ms: globalTimeoutMs,
    global_timeout_reached: globalTimeoutReached,
    panel_url: panelUrl,
    browser_mode: browserUsed,
    profile_count: summaryProfiles.length,
    profiles: summaryProfiles,
    draft_isolation: {
      ok: draftIsolation.ok === true,
      skipped: draftIsolation.skipped === true,
      server_unchanged: draftIsolation.server_unchanged === true,
      series_draft_preserved: draftIsolation.series_draft_preserved === true,
      movies_draft_clean: draftIsolation.movies_draft_clean === true,
      draft_reverted: draftIsolation.draft_reverted === true,
      error: draftIsolation.error || null,
      reason: draftIsolation.reason || null,
    },
    cas_series: {
      ok: casSeries.ok === true,
      skipped: casSeries.skipped === true,
      mutation_kind: casSeries.mutation_kind || null,
      original_series_fingerprint: casSeries.original_series_fingerprint || null,
      mutation_fingerprint: casSeries.mutation_fingerprint || null,
      conflict_status: casSeries.conflict_status || null,
      conflict_error: casSeries.conflict_error || null,
      stale_draft_preserved: casSeries.stale_draft_preserved === true,
      restoration_ok: casSeries.restoration && casSeries.restoration.ok === true,
      exact_rules_restored: casSeries.restoration && casSeries.restoration.exact_rules_restored === true,
      exact_fingerprint_restored: casSeries.restoration && casSeries.restoration.exact_fingerprint_restored === true,
      movie_unchanged: casSeries.movie_unchanged === true,
      error: casSeries.error || null,
      reason: casSeries.reason || null,
    },
    console_error_count: consoleErrors.length,
    page_error_count: pageErrors.length,
    blocking_network_count: blockingNetwork.length,
    expected_network_count: network.filter(event => event.expected === true).length,
    download_count: downloads.length,
    forbidden_action_count: forbiddenActions.length,
    screenshots: summaryProfiles.map(profile => profile.screenshot),
    launch_error: launchError,
  };

  writeJson("summary.json", summary);
  writeJson("routes.json", Object.fromEntries(profiles.map(profile => [
    profile.profile,
    profile.details ? profile.details.route_checks : [],
  ])));
  writeJson("interactions.json", Object.fromEntries(profiles.map(profile => [
    profile.profile,
    profile.details ? profile.details.interactions : [],
  ])));
  writeJson("persistence.json", Object.fromEntries(profiles.map(profile => [
    profile.profile,
    profile.details ? profile.details.persistence : [],
  ])));
  writeJson("layouts.json", Object.fromEntries(profiles.map(profile => [
    profile.profile,
    profile.details ? profile.details.layouts : [],
  ])));
  writeJson("draft_isolation.json", draftIsolation);
  writeJson("cas_series.json", casSeries);
  writeJson("console.json", consoleEvents);
  writeJson("page_errors.json", pageErrors);
  writeJson("network.json", network);
  writeJson("downloads.json", downloads);
  writeJson("forbidden_actions.json", forbiddenActions);
  clearTimeout(globalWatchdog);

  console.log("ARR_UI_CHECK_JSON_START");
  console.log(JSON.stringify(summary, null, 2));
  console.log("ARR_UI_CHECK_JSON_END");
  if (!ok) {
    process.exitCode = 1;
    console.error("ARR_UI_CHECK_FAILED");
    return;
  }
  console.log("ARR_UI_CHECK_OK");
})();
'@

$script | Set-Content -LiteralPath $runner -Encoding UTF8

$previousErrorAction = $ErrorActionPreference
Push-Location $runtimeDir
try {
  $keep = if ($KeepBrowserOpen) { "1" } else { "0" }
  $ErrorActionPreference = "Continue"
  $output = & $node.Source $runner $PanelUrl $artifactDir $TimeoutMs $keep $Browser 2>&1
  $exitCode = $LASTEXITCODE
} finally {
  $ErrorActionPreference = $previousErrorAction
  Pop-Location
}

$transcript = Join-Path $artifactDir "transcript.txt"
$output | Set-Content -LiteralPath $transcript -Encoding UTF8
$output | ForEach-Object { Write-Host $_ }

$stopwatch.Stop()
Write-Host "ARR_UI_CHECK_TOTAL_MS=$($stopwatch.ElapsedMilliseconds)"

if ($exitCode -ne 0) {
  throw "ARR_UI_CHECK_FAILED exit=$exitCode artifact=$artifactDir"
}
if (($output -join [Environment]::NewLine) -notmatch "ARR_UI_CHECK_OK") {
  throw "ARR_UI_CHECK_NO_OK artifact=$artifactDir"
}

Write-Host "ARR_UI_CHECK_ARTIFACT $artifactDir"
Write-Host "ARR_UI_CHECK_DONE"
