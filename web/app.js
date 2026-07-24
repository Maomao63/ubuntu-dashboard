const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
let overview = null;
let busy = false;
let liveTimer = null;
let failedUpdates = 0;
const LIVE_INTERVAL_MS = 500;
let selectedShare = null;
let selectedPath = "";
let currentPage = "overview";
let currentLanguage = localStorage.getItem("ubuntu-dashboard-language") || "en";
let csrfToken = "";
let sessionInfo = null;
let sshSocket = null;
let sshTerminal = null;
let sshFit = null;
let dockerUpdates = {};
let networkHistory = {down: [], up: []};
let metricHistory = {cpu: [], memory: []};

function t(key) {
  return window.I18N?.[currentLanguage]?.[key] ?? window.I18N?.en?.[key] ?? key;
}

function applyLanguage(language) {
  currentLanguage = window.I18N?.[language] ? language : "en";
  localStorage.setItem("ubuntu-dashboard-language", currentLanguage);
  document.documentElement.lang = currentLanguage;
  $("#language-select").value = currentLanguage;
  $$("[data-i18n]").forEach(element => element.textContent = t(element.dataset.i18n));
  updatePageHeading();
  updateClock();
  if (overview) render(overview);
  if (currentPage === "shares") loadShares(selectedShare, selectedPath);
  if (sessionInfo) {
    loadVersionCheck();
  }
}

function updatePageHeading() {
  $("#page-kicker").textContent = t(`kicker.${currentPage}`);
  $("#page-title").textContent = t(`page.${currentPage}`);
}

function updateClock() {
  const now = new Date();
  $("#clock-time").textContent = now.toLocaleTimeString(currentLanguage, {
    hour: "2-digit", minute: "2-digit", second: "2-digit"
  });
  $("#clock-date").textContent = now.toLocaleDateString(currentLanguage, {
    weekday: "short", day: "2-digit", month: "short"
  });
}

$("#language-select").addEventListener("change", event => applyLanguage(event.target.value));
setInterval(updateClock, 1000);

const escapeHtml = (value = "") => String(value).replace(/[&<>"']/g, char => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
}[char]));
const svgIcon = (name, extra = "") =>
  `<svg class="ui-icon ${extra}" aria-hidden="true"><use href="#icon-${name}"></use></svg>`;

const bytes = (value = 0, speed = false) => {
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let number = Number(value), unit = 0;
  while (number >= 1024 && unit < units.length - 1) { number /= 1024; unit++; }
  return `${number >= 100 || unit === 0 ? number.toFixed(0) : number.toFixed(1)} ${units[unit]}${speed ? "/s" : ""}`;
};

const duration = (seconds = 0) => {
  seconds = Number(seconds);
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor(seconds % 86400 / 3600);
  const mins = Math.floor(seconds % 3600 / 60);
  return days
    ? `${days} ${t("unit.days")} ${hours} ${t("unit.hours")}`
    : hours
      ? `${hours} ${t("unit.hours")} ${mins} ${t("unit.minutes")}`
      : `${mins} ${t("unit.minutes")}`;
};

const toast = (message, error = false) => {
  const element = $("#toast");
  element.textContent = message;
  element.className = error ? "show error" : "show";
  clearTimeout(element.timer);
  element.timer = setTimeout(() => element.className = "", 2800);
};

function setPage(name) {
  if (!["overview", "docker", "storage", "shares", "processes", "logs", "cli", "settings"].includes(name)) name = "overview";
  currentPage = name;
  if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
  $$(".page").forEach(page => page.classList.toggle("active", page.id === `page-${name}`));
  $$(".nav").forEach(nav => nav.classList.toggle("active", nav.dataset.page === name));
  updatePageHeading();
  document.body.classList.remove("menu-open");
  if (name === "processes") loadProcesses();
  if (name === "logs") loadLogs();
  if (name === "shares") loadShares(selectedShare, selectedPath);
  if (name === "settings") loadAccountSettings();
  if (name === "cli") {
    requestAnimationFrame(() => {
      sshFit?.fit();
      if (sshSocket?.readyState === WebSocket.OPEN) sshTerminal?.focus();
      else $("#ssh-username").focus();
    });
  }
}

$$(".nav").forEach(button => button.addEventListener("click", () => setPage(button.dataset.page)));
$$("[data-jump]").forEach(button => button.addEventListener("click", () => setPage(button.dataset.jump)));
$$(".mobile-menu").forEach(button => button.addEventListener("click", () => document.body.classList.toggle("menu-open")));
$("#refresh").addEventListener("click", () => loadOverview(true));

function setupWidgetLayout() {
  const toggle = $("#layout-edit");
  const groups = [];

  function createGroup(container, selector, attribute, storageKey, draggable = true, section = false) {
    const cards = () => [...container.querySelectorAll(`:scope > ${selector}`)];
    let saved = [];
    try { saved = JSON.parse(localStorage.getItem(storageKey) || "[]"); }
    catch { localStorage.removeItem(storageKey); }
    saved.forEach(id => {
      const card = cards().find(item => item.dataset[attribute] === id);
      if (card) container.append(card);
    });
    const save = () => localStorage.setItem(storageKey, JSON.stringify(cards().map(card => card.dataset[attribute])));
    cards().forEach(card => {
      const controls = document.createElement("span");
      controls.className = section ? "section-move" : "widget-move";
      controls.innerHTML = `<button data-move="-1" title="Move left/up">←</button><button data-move="1" title="Move right/down">→</button>`;
      card.append(controls);
      controls.addEventListener("click", event => {
        const button = event.target.closest("[data-move]");
        if (!button) return;
        event.stopPropagation();
        const all = cards(), index = all.indexOf(card);
        const target = all[index + Number(button.dataset.move)];
        if (!target) return;
        if (Number(button.dataset.move) < 0) container.insertBefore(card, target);
        else container.insertBefore(target, card);
        save();
      });
      if (!draggable) return;
      card.addEventListener("dragstart", event => {
        if (!toggle.checked) return event.preventDefault();
        card.classList.add("dragging");
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", card.dataset[attribute]);
      });
      card.addEventListener("dragend", () => {
        cards().forEach(item => item.classList.remove("dragging", "drag-over"));
        save();
      });
      card.addEventListener("dragover", event => {
        if (!toggle.checked) return;
        event.preventDefault();
        cards().forEach(item => item.classList.remove("drag-over"));
        card.classList.add("drag-over");
      });
      card.addEventListener("drop", event => {
        event.preventDefault();
        const id = event.dataTransfer.getData("text/plain");
        const moving = cards().find(item => item.dataset[attribute] === id);
        if (!moving || moving === card) return;
        const rect = card.getBoundingClientRect();
        const before = event.clientX < rect.left + rect.width / 2;
        container.insertBefore(moving, before ? card : card.nextSibling);
        save();
      });
    });
    groups.push({
      container, cards, draggable,
      setEditing(enabled) {
        container.classList.toggle("editing", enabled);
        cards().forEach(card => card.draggable = draggable && enabled);
      }
    });
  }

  createGroup($("#page-overview"), "[data-overview-section]", "overviewSection", "ubuntu-dashboard-sections", false, true);
  createGroup($(".metrics"), ".metric", "widget", "ubuntu-dashboard-widgets");
  createGroup($(".dashboard-grid"), ".panel", "panelWidget", "ubuntu-dashboard-panels");
  toggle.addEventListener("change", () => {
    groups.forEach(group => group.setEditing(toggle.checked));
    toast(toggle.checked ? t("layout.unlocked") : t("layout.saved"));
  });
  groups.forEach(group => group.setEditing(false));
}

setupWidgetLayout();
$("#distro-logo").addEventListener("error", event => {
  event.currentTarget.style.display = "none";
});

function storageRows(items, limit = items.length) {
  if (!items.length) return `<div class="empty-state">${t("common.noDrives")}</div>`;
  return `<div class="overview-table-head"><span>Pool</span><span>Used</span><span>Free</span><span>Usage</span></div>${items.slice(0, limit).map(group => `
    <button class="storage-pool-row" data-jump="storage">
      <span><i class="disk-health ${group.status}"></i><b>${escapeHtml(group.name)}</b><small>${escapeHtml(group.type)}</small></span>
      <strong>${bytes(group.used)}</strong><strong>${bytes(group.available)}</strong>
      <span class="pool-usage"><b>${group.percent}%</b><i><em style="width:${Math.min(group.percent, 100)}%"></em></i></span>
    </button>`).join("")}`;
}

function stackMini(items) {
  if (!items.length) return `<div class="empty-state">${t("docker.noStacks")}</div>`;
  return `<div class="overview-table-head stack-head"><span>Name</span><span>Status</span><span>Containers</span></div>${items.map(item => {
    const updateAvailable = item.containerIds.some(id => dockerUpdates[id]?.updateAvailable);
    return `
    <button class="stack-row" data-jump="docker">
      <span>${svgIcon("layers", "stack-cube")}<b>${escapeHtml(item.name)}</b>${updateAvailable ? `<i class="image-update-badge">${svgIcon("refresh")}</i>` : ""}</span>
      <span class="stack-state"><i class="state-dot ${item.health}"></i>${escapeHtml(t(`health.${item.health}`))}</span>
      <strong>${item.running} / ${item.total}</strong>
    </button>`;
  }).join("")}`;
}

function renderContainers(docker) {
  $("#docker-summary").innerHTML = `
    <span class="chip"><strong>${docker.containersRunning || 0}</strong> aktiv</span>
    <span class="chip"><strong>${docker.containersStopped || 0}</strong> gestoppt</span>
    <span class="chip"><strong>${docker.images || 0}</strong> Images</span>`;
  if (!docker.available) {
    $("#container-table").innerHTML = `<div class="error-box">Docker nicht erreichbar: ${escapeHtml(docker.error)}</div>`;
    return;
  }
  if (!docker.containers.length) {
    $("#container-table").innerHTML = `<div class="error-box">Noch keine Container vorhanden.</div>`;
    return;
  }
  $("#container-table").innerHTML = `<table>
    <thead><tr><th>Container</th><th>Status</th><th>Ports</th><th>Erstellt</th><th>Aktionen</th></tr></thead>
    <tbody>${docker.containers.map(item => `
      <tr>
        <td><div class="container-name"><span class="cube">⬡</span><div><strong>${escapeHtml(item.name)} ${dockerUpdates[item.fullId]?.updateAvailable ? `<i class="image-update-badge inline" title="${escapeHtml(t("docker.updateAvailable"))}">↻</i>` : ""}</strong><small>${escapeHtml(item.image)}</small></div></div></td>
        <td><span class="state ${item.state}"><i class="state-dot ${item.health || item.state}"></i>${escapeHtml(item.health || item.state)}</span><br><small>${escapeHtml(item.status)}</small></td>
        <td>${escapeHtml(item.ports.join(", ") || "–")}</td>
        <td>${new Date(item.created * 1000).toLocaleDateString("de-DE")}</td>
        <td><div class="actions">
          ${item.isSelf
            ? `<span class="chip">Dashboard</span>`
            : item.state === "running"
            ? `<button class="action danger" data-action="stop" data-id="${item.fullId}">Stop</button><button class="action" data-action="restart" data-id="${item.fullId}">Restart</button>`
            : `<button class="action" data-action="start" data-id="${item.fullId}">Start</button>`}
        </div></td>
      </tr>`).join("")}</tbody>
  </table>`;
  $$("[data-action]").forEach(button => button.addEventListener("click", () => dockerAction(button)));
}

function render(data) {
  overview = data;
  const system = data.system, docker = data.docker;
  $("#version").textContent = `v${data.version} · latest`;
  const brandNames = {
    ubuntu: "UBUNTU", debian: "DEBIAN", fedora: "FEDORA", arch: "ARCH",
    manjaro: "MANJARO", linuxmint: "MINT", opensuse: "OPENSUSE",
    "opensuse-tumbleweed": "OPENSUSE", rocky: "ROCKY", rhel: "RHEL",
    almalinux: "ALMALINUX", unraid: "UNRAID"
  };
  $("#distro-name").textContent = brandNames[system.distro.id] || "LINUX";
  $("#distro-logo").style.display = "block";
  $("#distro-logo").src = `https://cdn.simpleicons.org/${encodeURIComponent(system.distro.icon)}/${system.distro.color.replace("#", "")}`;
  $("#hero-distro-logo").src = $("#distro-logo").src;
  document.documentElement.style.setProperty("--brand", system.distro.color);
  if (!sshSocket || sshSocket.readyState !== WebSocket.OPEN) {
    $("#terminal-title").textContent = `${system.distro.id}-control@${system.hostname}:~`;
  }
  $("#hostname").textContent = system.hostname;
  $("#os").textContent = system.os;
  $("#kernel").textContent = system.kernel;
  $("#uptime").textContent = duration(system.uptime);
  $("#cpu-value").textContent = `${system.cpu.percent}%`;
  $("#cpu-sub").textContent = `${system.cpu.cores} ${t("common.cores")} · ${system.cpu.model}`;
  $("#cpu-ring-value").textContent = Math.round(system.cpu.percent);
  $("#cpu-ring").style.setProperty("--value", system.cpu.percent);
  $("#ram-value").textContent = `${system.memory.percent}%`;
  $("#ram-sub").textContent = `${bytes(system.memory.used)} ${t("common.of")} ${bytes(system.memory.total)}`;
  $("#ram-ring-value").textContent = Math.round(system.memory.percent);
  $("#ram-ring").style.setProperty("--value", system.memory.percent);
  metricHistory.cpu.push(system.cpu.percent);
  metricHistory.memory.push(system.memory.percent);
  metricHistory.cpu = metricHistory.cpu.slice(-30);
  metricHistory.memory = metricHistory.memory.slice(-30);
  const percentPoints = values => Array(30 - values.length).fill(0).concat(values)
    .map((value, index) => `${index * 120 / 29},${32 - value / 100 * 29}`).join(" ");
  $("#cpu-line").setAttribute("points", percentPoints(metricHistory.cpu));
  $("#ram-line").setAttribute("points", percentPoints(metricHistory.memory));
  networkHistory.down.push(system.network.down);
  networkHistory.up.push(system.network.up);
  networkHistory.down = networkHistory.down.slice(-30);
  networkHistory.up = networkHistory.up.slice(-30);
  const networkPeak = Math.max(...networkHistory.down, ...networkHistory.up, 1);
  const chartPoints = values => {
    const padded = Array(30 - values.length).fill(0).concat(values);
    return padded.map((value, index) => `${index * 120 / 29},${32 - value / networkPeak * 29}`).join(" ");
  };
  $("#network-down-line").setAttribute("points", chartPoints(networkHistory.down));
  $("#network-up-line").setAttribute("points", chartPoints(networkHistory.up));
  $("#network-down").textContent = bytes(system.network.down, true);
  $("#network-up").textContent = bytes(system.network.up, true);
  $("#network-interface").textContent = system.network.interfaces.join(", ") || "–";
  $("#docker-value").textContent = docker.available ? `${docker.stacks.length} ${t("docker.stacks")}` : "Offline";
  $("#docker-sub").textContent = docker.available ? `${docker.stacks.reduce((sum, stack) => sum + stack.running, 0)}/${docker.stacks.reduce((sum, stack) => sum + stack.total, 0)} ${t("common.containers")} · Docker ${docker.version}` : docker.error;
  const dockerTotal = docker.available ? docker.stacks.reduce((sum, stack) => sum + stack.total, 0) : 0;
  const dockerRunning = docker.available ? docker.stacks.reduce((sum, stack) => sum + stack.running, 0) : 0;
  const dockerPercent = dockerTotal ? Math.round(dockerRunning / dockerTotal * 100) : 0;
  $("#docker-ring").style.setProperty("--value", dockerPercent);
  $("#docker-ring-value").textContent = dockerPercent;
  $("#architecture").textContent = system.architecture;
  $("#cores").textContent = system.cpu.cores;
  $("#hero-architecture").textContent = system.architecture;
  $("#load-average").textContent = system.load.join("  ");
  $("#process-count").textContent = system.processCount ?? "–";
  const root = system.rootFilesystem || {};
  $("#root-usage").textContent = `${root.percent || 0}%`;
  $("#root-usage-sub").textContent = `${bytes(root.used || 0)} / ${bytes(root.total || 0)}`;
  const swapPercent = system.memory.swapTotal ? Math.round(system.memory.swapUsed / system.memory.swapTotal * 100) : 0;
  $("#swap-usage").textContent = `${swapPercent}%`;
  $("#swap-usage-sub").textContent = `${bytes(system.memory.swapUsed)} / ${bytes(system.memory.swapTotal)}`;
  const diskStates = system.disks.map(disk => disk.health);
  const health = !docker.available || docker.health === "unhealthy" || diskStates.includes("critical")
    ? "critical" : docker.health === "warning" || diskStates.includes("warning") ? "warning" : "healthy";
  const healthText = health === "healthy" ? t("health.operational") : health === "warning" ? t("health.warnings") : t("health.attention");
  $("#global-health").className = `global-health ${health}`;
  $("#global-health-label").textContent = healthText;
  $("#hero-health").textContent = health === "healthy" ? "Healthy" : health === "warning" ? "Warning" : "Critical";
  $("#container-preview").classList.remove("skeleton-block");
  $("#container-preview").innerHTML = docker.available ? stackMini(docker.stacks) : `<div class="empty-state">${escapeHtml(docker.error)}</div>`;
  $("#container-preview").onclick = event => {
    if (event.target.closest("[data-jump]")) setPage("docker");
  };
  $("#storage-preview").classList.remove("skeleton-block");
  $("#storage-preview").innerHTML = storageRows(data.storage);
  $("#storage-preview").onclick = event => {
    if (event.target.closest("[data-jump]")) setPage("storage");
  };
  $("#temperatures").innerHTML = system.disks.length
    ? system.disks.map(disk => `<div class="temp-row">
        <i class="disk-health ${disk.health}" title="${escapeHtml(t(`health.${disk.health}`))}"></i>
        <span><b>${escapeHtml(disk.name)}</b><small>/dev/${escapeHtml(disk.device)} · ${escapeHtml(disk.model)} · ${escapeHtml(t(`disk.${disk.state}`))} · ${escapeHtml(t(`health.${disk.health}`))}</small></span>
        <strong>${disk.temperature === null ? "–" : `${disk.temperature} °C`}</strong>
      </div>`).join("")
    : `<span>${t("common.noDrives")}</span>`;
  $("#temperatures").classList.toggle("empty-state", !system.disks.length);
  $("#storage-cards").innerHTML = data.storage.length ? data.storage.map(group => `
    <article class="storage-card">
      <div class="storage-card-head">
        <div><h3><i class="disk-health ${group.status}"></i>${escapeHtml(group.name)}</h3><p>${escapeHtml(group.type)} · ${group.members.length} ${t("storage.drives")}</p></div>
        <strong>${group.percent}%</strong>
      </div>
      <div class="big-bar"><i style="width:${Math.min(group.percent, 100)}%"></i></div>
      <div class="storage-stats"><span>${bytes(group.used)} ${t("storage.used")}</span><span>${bytes(group.available)} ${t("common.free")} · ${bytes(group.total)} ${t("storage.total")}</span></div>
      <div class="storage-member-list">${group.members.map(member => `
        <div class="storage-member">
          <i class="disk-health ${member.status}"></i>
          <div><b>${escapeHtml(member.name)}</b><small>${escapeHtml(member.vdev || member.role)} · /dev/${escapeHtml(member.device)}</small></div>
          <span>${member.temperature === null ? "" : `${member.temperature} °C · `}${member.used !== undefined && member.role === "data" ? `${bytes(member.used)} / ${bytes(member.total || member.size)}` : bytes(member.size)}</span>
        </div>`).join("")}
      </div>
    </article>`).join("") : `<div class="error-box">${t("common.noDrives")}</div>`;
  renderContainers(docker);
}

async function loadOverview(manual = false) {
  if (busy) return scheduleLiveUpdate(500);
  busy = true;
  $("#refresh").classList.add("spinning");
  try {
    const response = await fetch("/api/overview", {cache: "no-store"});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
    failedUpdates = 0;
    $(".sidebar-live").classList.remove("offline");
    $("#live-status").textContent = `${t("live.tickrate")} · ${LIVE_INTERVAL_MS} ms`;
    if (manual) toast("Daten wurden aktualisiert");
  } catch (error) {
    failedUpdates++;
    toast(`Verbindung fehlgeschlagen: ${error.message}`, true);
    $(".sidebar-live").classList.add("offline");
    $("#live-status").textContent = t("live.disconnected");
  } finally {
    busy = false;
    $("#refresh").classList.remove("spinning");
    scheduleLiveUpdate(document.hidden ? 5000 : failedUpdates ? 3000 : LIVE_INTERVAL_MS);
  }
}

async function loadVersionCheck() {
  const status = $("#version-check");
  try {
    const response = await fetch("/api/version", {cache: "no-store"});
    const data = await response.json();
    status.classList.toggle("update", data.updateAvailable);
    status.querySelector("i").textContent = data.updateAvailable ? "!" : "✓";
    status.querySelector("span").textContent = data.updateAvailable ? t("version.available") : t("version.latest");
    status.title = data.latest ? `${t("version.github")}: v${data.latest}` : t("version.unavailable");
  } catch {
    status.classList.add("unknown");
    status.querySelector("i").textContent = "?";
    status.querySelector("span").textContent = t("version.unavailable");
  }
}

async function loadDockerUpdates() {
  try {
    const response = await fetch("/api/docker-updates", {cache: "no-store"});
    const data = await response.json();
    dockerUpdates = data.containers || {};
    if (overview) render(overview);
  } catch {
    dockerUpdates = {};
  }
}

function scheduleLiveUpdate(delay = LIVE_INTERVAL_MS) {
  clearTimeout(liveTimer);
  liveTimer = setTimeout(() => loadOverview(), delay);
}

async function dockerAction(button) {
  const actionNames = {start: "starten", stop: "stoppen", restart: "neu starten"};
  const action = button.dataset.action;
  if ((action === "stop" || action === "restart") &&
      !window.confirm(`Container wirklich ${actionNames[action]}?`)) return;
  button.disabled = true;
  button.textContent = "…";
  try {
    const response = await fetch(`/api/docker/${button.dataset.id}/${action}`, {
      method: "POST",
      headers: {"X-CSRF-Token": csrfToken}
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Aktion fehlgeschlagen");
    toast(`Container wird ${actionNames[action]}`);
    setTimeout(() => loadOverview(), 900);
  } catch (error) {
    toast(error.message, true);
    button.disabled = false;
    button.textContent = action;
  }
}

function shareUrl(share, path = "") {
  const params = new URLSearchParams();
  if (share !== null && share !== undefined) params.set("share", share);
  if (path) params.set("path", path);
  return `/api/files?${params}`;
}

async function loadShares(share = null, path = "") {
  try {
    const response = await fetch(shareUrl(share, path), {cache: "no-store"});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Freigaben konnten nicht geladen werden");
    selectedShare = data.selected ?? share;
    selectedPath = data.relative || "";
    if (data.selected === undefined && data.shares.length) {
      await loadShares(data.shares[0].id, "");
      return;
    }
    $("#new-folder").disabled = data.selected === undefined;
    $("#new-file").disabled = data.selected === undefined;
    $("#location-count").textContent = data.shares.length;
    $("#share-list").classList.remove("loading");
    $("#share-list").innerHTML = data.shares.length ? data.shares.map(item => `
      <button class="share-button ${Number(selectedShare) === item.id ? "active" : ""}" data-share="${item.id}">
        <span class="share-folder">${svgIcon("folder")}</span>
        <span><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.protocol)} · ${bytes(item.free)} ${t("common.free")}</small></span>
      </button>`).join("") : `<div class="empty-state">${t("shares.none")}<br><small>SHARE_ROOTS</small></div>`;
    $$("[data-share]").forEach(button => button.addEventListener("click", () => loadShares(Number(button.dataset.share), "")));
    renderFiles(data);
  } catch (error) {
    $("#file-list").innerHTML = `<div class="error-box">${escapeHtml(error.message)}</div>`;
  }
}

function renderFiles(data) {
  if (data.selected === undefined) return;
  const share = data.shares.find(item => item.id === Number(data.selected));
  const used = Math.max(0, (share?.total || 0) - (share?.free || 0));
  const percent = share?.total ? Math.round(used / share.total * 100) : 0;
  $("#browser-root-name").textContent = share?.name || "Unknown location";
  $("#browser-root-protocol").textContent = share?.protocol || "–";
  $("#browser-root-capacity").textContent = share?.total ? `${bytes(used)} ${t("common.usedOf")} ${bytes(share.total)}` : t("browser.capacityUnavailable");
  $("#browser-root-percent").textContent = share?.total ? `${percent}%` : "–";
  $("#browser-root-bar").style.width = `${percent}%`;
  const segments = (data.relative || "").split("/").filter(Boolean);
  let accumulated = "";
  const crumbs = [`<button data-browse-path="">${escapeHtml(share?.name || "Freigabe")}</button>`];
  segments.forEach(segment => {
    accumulated += `${accumulated ? "/" : ""}${segment}`;
    crumbs.push(`<i>/</i><button data-browse-path="${escapeHtml(accumulated)}">${escapeHtml(segment)}</button>`);
  });
  $("#share-breadcrumbs").innerHTML = crumbs.join("");
  $("#share-count").textContent = `${data.entries.length}${data.truncated ? "+" : ""} ${t("common.entries")}`;
  $("#share-up").disabled = !data.relative;
  $("#share-up").onclick = () => {
    const parent = segments.slice(0, -1).join("/");
    loadShares(selectedShare, parent);
  };
  $$("[data-browse-path]").forEach(button => button.addEventListener("click", () => loadShares(selectedShare, button.dataset.browsePath)));
  $("#file-list").innerHTML = data.entries.length ? data.entries.map(item => {
    const nextPath = [data.relative, item.name].filter(Boolean).join("/");
    return `<div class="file-row">
      <div class="file-name">
        <span class="file-icon ${item.type === "directory" ? "folder" : ""}">${svgIcon(item.type === "directory" ? "folder" : "file")}</span>
        ${item.type === "directory"
          ? `<button data-open-path="${escapeHtml(nextPath)}">${escapeHtml(item.name)}</button>`
          : `<button data-edit-path="${escapeHtml(nextPath)}">${escapeHtml(item.name)}</button>`}
      </div>
      <span class="file-meta owner">${escapeHtml(item.owner)}<small>${escapeHtml(item.group)}</small></span>
      <span class="file-permissions" title="${escapeHtml(item.mode)}">${escapeHtml(item.permissions)} <small>(${escapeHtml(item.mode)})</small></span>
      <span class="file-meta">${item.type === "directory" ? t("common.folder") : bytes(item.size)}</span>
      <span class="file-meta modified">${new Date(item.modified * 1000).toLocaleString(currentLanguage, {dateStyle: "medium", timeStyle: "short"})}</span>
      <span class="file-actions">
        ${item.type === "directory"
          ? `<button class="file-action" data-open-path="${escapeHtml(nextPath)}" title="${escapeHtml(t("common.open"))}">${svgIcon("open")}</button>`
          : `<button class="file-action" data-edit-path="${escapeHtml(nextPath)}" title="${escapeHtml(t("common.edit"))}">${svgIcon("edit")}</button>`}
        <button class="file-action danger" data-delete-path="${escapeHtml(nextPath)}" data-delete-name="${escapeHtml(item.name)}" title="${escapeHtml(t("common.delete"))}">${svgIcon("trash")}</button>
      </span>
    </div>`;
  }).join("") : `<div class="empty-state">${t("common.empty")}</div>`;
  $$("[data-open-path]").forEach(button => button.addEventListener("click", () => loadShares(selectedShare, button.dataset.openPath)));
  $$("[data-edit-path]").forEach(button => button.addEventListener("click", () => openTextEditor(button.dataset.editPath)));
  $$("[data-delete-path]").forEach(button => button.addEventListener("click", () => openDeleteDialog(button.dataset.deletePath, button.dataset.deleteName)));
}

async function fileAction(action, payload) {
  const response = await fetch(`/api/files/${action}`, {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-CSRF-Token": csrfToken},
    body: JSON.stringify({share: selectedShare, ...payload})
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "File operation failed");
  return data;
}

function openNewFileDialog() {
  $("#file-dialog-form").dataset.mode = "create";
  $("#file-dialog-form").dataset.path = selectedPath;
  $("#file-dialog-title").textContent = t("browser.newFileTitle");
  $("#file-name-field").hidden = false;
  $("#file-name").value = "";
  $("#file-content").value = "";
  $("#file-dialog-error").textContent = "";
  $("#file-dialog").showModal();
  $("#file-name").focus();
}

async function openTextEditor(path) {
  try {
    const params = new URLSearchParams({share: selectedShare, path});
    const response = await fetch(`/api/file?${params}`, {cache: "no-store"});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "File could not be opened");
    $("#file-dialog-form").dataset.mode = "save";
    $("#file-dialog-form").dataset.path = path;
    $("#file-dialog-title").textContent = data.name;
    $("#file-name-field").hidden = true;
    $("#file-content").value = data.content;
    $("#file-dialog-error").textContent = "";
    $("#file-dialog").showModal();
    $("#file-content").focus();
  } catch (error) {
    toast(error.message, true);
  }
}

let deleteTarget = null;
function openDeleteDialog(path, name) {
  deleteTarget = path;
  $("#delete-name").textContent = name;
  $("#delete-dialog").showModal();
}

$("#new-file").addEventListener("click", openNewFileDialog);
$("#browser-refresh").addEventListener("click", () => loadShares(selectedShare, selectedPath));
$("#file-content").addEventListener("keydown", event => {
  if (event.key !== "Tab") return;
  event.preventDefault();
  const field = event.currentTarget;
  const start = field.selectionStart;
  field.setRangeText("  ", start, field.selectionEnd, "end");
});
$("#new-folder").addEventListener("click", () => {
  $("#folder-name").value = "";
  $("#folder-dialog-error").textContent = "";
  $("#folder-dialog").showModal();
  $("#folder-name").focus();
});

$$("[data-close-dialog]").forEach(button => button.addEventListener("click", () => {
  document.getElementById(button.dataset.closeDialog).close();
}));

$("#file-dialog-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector(".dialog-primary");
  button.disabled = true;
  try {
    if (form.dataset.mode === "create") {
      await fileAction("create", {path: selectedPath, name: $("#file-name").value, content: $("#file-content").value});
    } else {
      await fileAction("save", {path: form.dataset.path, content: $("#file-content").value});
    }
    $("#file-dialog").close();
    await loadShares(selectedShare, selectedPath);
    toast(t("browser.saved"));
  } catch (error) {
    $("#file-dialog-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

$("#folder-dialog-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.currentTarget.querySelector(".dialog-primary");
  button.disabled = true;
  try {
    await fileAction("mkdir", {path: selectedPath, name: $("#folder-name").value});
    $("#folder-dialog").close();
    await loadShares(selectedShare, selectedPath);
    toast(t("browser.created"));
  } catch (error) {
    $("#folder-dialog-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

$("#delete-dialog-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.currentTarget.querySelector(".dialog-danger");
  button.disabled = true;
  try {
    await fileAction("delete", {path: deleteTarget});
    $("#delete-dialog").close();
    await loadShares(selectedShare, selectedPath);
    toast(t("browser.deleted"));
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    deleteTarget = null;
  }
});

async function loadProcesses() {
  try {
    const response = await fetch("/api/processes");
    const data = await response.json();
    $("#process-table").innerHTML = `<table>
      <thead><tr><th>PID</th><th>Prozess</th><th>Status</th><th>Benutzer-ID</th><th>Speicher</th></tr></thead>
      <tbody>${data.processes.map(item => `<tr><td>${item.pid}</td><td>${escapeHtml(item.name)}</td><td>${escapeHtml(item.state)}</td><td>${escapeHtml(item.user)}</td><td>${bytes(item.memory)}</td></tr>`).join("")}</tbody>
    </table>`;
  } catch (error) { $("#process-table").textContent = error.message; }
}

async function loadLogs() {
  try {
    const response = await fetch("/api/logs");
    const data = await response.json();
    $("#log-source").textContent = `Quelle: ${data.source}`;
    $("#log-output").textContent = data.lines.join("\n");
    $("#log-output").scrollTop = $("#log-output").scrollHeight;
  } catch (error) { $("#log-output").textContent = error.message; }
}

function setSshStatus(state, text) {
  $("#ssh-status").className = `ssh-status ${state}`;
  $("#ssh-status").innerHTML = `<i></i> ${escapeHtml(text)}`;
}

function closeSsh(showLogin = true) {
  if (sshSocket) {
    const socket = sshSocket;
    sshSocket = null;
    if (socket.readyState < WebSocket.CLOSING) socket.close();
  }
  $("#terminal-disconnect").disabled = true;
  setSshStatus("", t("cli.disconnected"));
  if (showLogin) $("#ssh-login").classList.remove("hidden");
}

function setupSshTerminal() {
  if (!window.Terminal || !window.FitAddon) {
    $("#ssh-error").textContent = "Terminal-Komponente konnte nicht geladen werden.";
    return;
  }
  sshTerminal = new Terminal({
    cursorBlink: true,
    convertEol: false,
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
    fontSize: 14,
    lineHeight: 1.18,
    scrollback: 5000,
    theme: {
      background: "#090c10", foreground: "#d7dde5", cursor: "#f0764f",
      selectionBackground: "#f0764f55", black: "#11161c", brightBlack: "#697380"
    }
  });
  sshFit = new FitAddon.FitAddon();
  sshTerminal.loadAddon(sshFit);
  sshTerminal.open($("#ssh-terminal"));
  sshTerminal.writeln("\x1b[38;5;245mReady for an encrypted SSH connection to the host.\x1b[0m");
  sshTerminal.onData(data => {
    if (sshSocket?.readyState === WebSocket.OPEN) {
      sshSocket.send(JSON.stringify({type: "input", data}));
    }
  });
  const resize = () => {
    if (!sshFit || currentPage !== "cli") return;
    sshFit.fit();
    if (sshSocket?.readyState === WebSocket.OPEN) {
      sshSocket.send(JSON.stringify({type: "resize", cols: sshTerminal.cols, rows: sshTerminal.rows}));
    }
  };
  new ResizeObserver(resize).observe($("#ssh-terminal"));
  window.addEventListener("resize", resize);
}

$("#ssh-login").addEventListener("submit", event => {
  event.preventDefault();
  if (sshSocket) closeSsh(false);
  const username = $("#ssh-username").value.trim();
  const passwordField = $("#ssh-password");
  const password = passwordField.value;
  $("#ssh-error").textContent = "";
  $("#ssh-login button").disabled = true;
  setSshStatus("connecting", t("cli.connecting"));
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${location.host}/ws/ssh`);
  socket.binaryType = "arraybuffer";
  sshSocket = socket;
  socket.addEventListener("open", () => {
    sshFit.fit();
    socket.send(JSON.stringify({
      type: "auth", username, password,
      cols: sshTerminal.cols, rows: sshTerminal.rows
    }));
    passwordField.value = "";
  });
  socket.addEventListener("message", event => {
    if (event.data instanceof ArrayBuffer) {
      const chunk = new Uint8Array(event.data);
      sshTerminal.write(chunk);
      return;
    }
    let message;
    try { message = JSON.parse(event.data); }
    catch { return; }
    if (message.type === "connected") {
      $("#ssh-login").classList.add("hidden");
      $("#terminal-disconnect").disabled = false;
      $("#terminal-title").textContent = `${message.username}@${message.host}:${message.port}`;
      setSshStatus("connected", t("cli.connected"));
      sshTerminal.focus();
    } else if (message.type === "error") {
      $("#ssh-error").textContent = message.message || "SSH-Verbindung fehlgeschlagen.";
      sshTerminal.writeln(`\r\n\x1b[31m${message.message || "SSH connection failed"}\x1b[0m`);
      closeSsh(true);
    } else if (message.type === "exit") {
      sshTerminal.writeln(`\r\n\x1b[38;5;245mSSH session ended (code ${message.code}).\x1b[0m`);
      closeSsh(true);
    }
  });
  socket.addEventListener("error", () => {
    $("#ssh-error").textContent = "WebSocket-Verbindung zum Dashboard fehlgeschlagen.";
  });
  socket.addEventListener("close", () => {
    if (sshSocket === socket) closeSsh(true);
    $("#ssh-login button").disabled = false;
  });
});

$("#terminal-disconnect").addEventListener("click", () => closeSsh(true));

async function loadAccountSettings() {
  try {
    const [accountResponse, notificationResponse] = await Promise.all([
      fetch("/api/account"),
      fetch("/api/notifications")
    ]);
    if (!accountResponse.ok) throw new Error("Account could not be loaded.");
    const account = await accountResponse.json();
    $("#account-username").value = account.username || "";
    $("#settings-account-name").textContent = account.username || "Account";
    $("#settings-avatar").textContent = (account.username || "A").charAt(0).toUpperCase();
    if (notificationResponse.ok) {
      const notifications = await notificationResponse.json();
      $("#notifications-enabled").checked = notifications.enabled;
      $("#notification-disks").checked = notifications.diskAlerts;
      $("#notification-containers").checked = notifications.containerAlerts;
      $("#notification-system").checked = notifications.systemAlerts;
      $("#notification-mention").value = notifications.mention || "";
      $("#notification-repeat").value = String(notifications.repeatMinutes || 60);
      $("#notification-webhook").value = "";
      $("#notification-webhook").placeholder = notifications.webhookConfigured
        ? t("notifications.configured")
        : "https://discord.com/api/webhooks/…";
      $("#webhook-state").textContent = notifications.webhookConfigured
        ? t("notifications.configuredHint")
        : t("notifications.notConfigured");
      $("#webhook-state").className = notifications.webhookConfigured ? "configured" : "";
      $("#notification-status").textContent = notifications.lastError
        ? `${t("notifications.lastError")}: ${notifications.lastError}`
        : notifications.lastSent
          ? `${t("notifications.lastSent")}: ${new Date(notifications.lastSent * 1000).toLocaleString(currentLanguage)}`
          : "";
    }
  } catch (error) {
    $("#account-error").textContent = error.message;
  }
}

function notificationPayload(clearWebhook = false) {
  return {
    enabled: $("#notifications-enabled").checked,
    webhookUrl: $("#notification-webhook").value.trim(),
    clearWebhook,
    mention: $("#notification-mention").value.trim(),
    diskAlerts: $("#notification-disks").checked,
    containerAlerts: $("#notification-containers").checked,
    systemAlerts: $("#notification-system").checked,
    repeatMinutes: Number($("#notification-repeat").value)
  };
}

async function saveNotifications(clearWebhook = false, showToast = true) {
  $("#notification-status").textContent = "";
  const response = await fetch("/api/notifications", {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-CSRF-Token": csrfToken},
    body: JSON.stringify(notificationPayload(clearWebhook))
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Notifications could not be saved.");
  $("#notification-webhook").value = "";
  if (clearWebhook) {
    $("#notifications-enabled").checked = false;
    $("#notification-webhook").placeholder = "https://discord.com/api/webhooks/…";
    $("#webhook-state").textContent = t("notifications.notConfigured");
    $("#webhook-state").className = "";
  } else if (data.webhookConfigured) {
    $("#notification-webhook").placeholder = t("notifications.configured");
    $("#webhook-state").textContent = t("notifications.configuredHint");
    $("#webhook-state").className = "configured";
  }
  if (showToast) toast(t("notifications.saved"));
  return data;
}

$("#notification-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.currentTarget.querySelector(".notification-save");
  button.disabled = true;
  try {
    await saveNotifications();
  } catch (error) {
    $("#notification-status").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

$("#notification-test").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    await saveNotifications(false, false);
    $("#notification-status").textContent = t("notifications.testing");
    const response = await fetch("/api/notifications/test", {
      method: "POST",
      headers: {"X-CSRF-Token": csrfToken}
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Discord test failed.");
    $("#notification-status").textContent = t("notifications.testSent");
    toast(t("notifications.testSent"));
  } catch (error) {
    $("#notification-status").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

$("#notification-remove").addEventListener("click", async event => {
  if (!window.confirm(t("notifications.removeConfirm"))) return;
  const button = event.currentTarget;
  button.disabled = true;
  $("#notifications-enabled").checked = false;
  try {
    await saveNotifications(true);
  } catch (error) {
    $("#notification-status").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

$("#account-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button");
  const newPassword = $("#account-new-password").value;
  const confirmation = $("#account-confirm-password").value;
  $("#account-error").textContent = "";
  if (newPassword !== confirmation) {
    $("#account-error").textContent = "The new passwords do not match.";
    return;
  }
  button.disabled = true;
  try {
    const response = await fetch("/api/account", {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": csrfToken},
      body: JSON.stringify({
        username: $("#account-username").value.trim(),
        currentPassword: $("#account-current-password").value,
        newPassword
      })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Account could not be saved.");
    csrfToken = data.csrf;
    sessionInfo.username = data.username;
    $("#settings-account-name").textContent = data.username;
    $("#settings-avatar").textContent = data.username.charAt(0).toUpperCase();
    $("#account-current-password").value = "";
    $("#account-new-password").value = "";
    $("#account-confirm-password").value = "";
    toast("Account updated. Other sessions were signed out.");
  } catch (error) {
    $("#account-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

$("#logout").addEventListener("click", async () => {
  closeSsh(false);
  await fetch("/api/logout", {method: "POST", headers: {"X-CSRF-Token": csrfToken}});
  location.replace("/login.html");
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) loadOverview();
  else scheduleLiveUpdate(5000);
});
window.addEventListener("online", () => loadOverview());

async function bootstrap() {
  try {
    const response = await fetch("/api/session");
    if (!response.ok) {
      location.replace("/login.html");
      return;
    }
    sessionInfo = await response.json();
    csrfToken = sessionInfo.csrf || "";
    $("#settings-account-name").textContent = sessionInfo.username || "Account";
    $("#settings-avatar").textContent = (sessionInfo.username || "A").charAt(0).toUpperCase();
    $("#account-username").value = sessionInfo.username || "";
    $("#ssh-host").value = `${sessionInfo.sshHost}:${sessionInfo.sshPort}`;
    applyLanguage(currentLanguage);
    setPage(location.hash.slice(1) || "overview");
    loadOverview();
    loadDockerUpdates();
    setInterval(loadVersionCheck, 15 * 60 * 1000);
    setInterval(loadDockerUpdates, 15 * 60 * 1000);
    try {
      setupSshTerminal();
    } catch (terminalError) {
      $("#ssh-error").textContent = `Terminal unavailable: ${terminalError.message}`;
    }
  } catch {
    location.replace("/login.html");
  }
}

bootstrap();
