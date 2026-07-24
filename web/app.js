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
  currentPage = name;
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
$(".mobile-menu").addEventListener("click", () => document.body.classList.toggle("menu-open"));
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
  return items.slice(0, limit).map(item => `
    <div class="storage-row">
      <div class="storage-name"><strong>${escapeHtml(item.mount)}</strong><small>${escapeHtml(item.device)} · ${escapeHtml(item.filesystem)}</small></div>
      <div class="bar"><i style="width:${Math.min(item.percent, 100)}%"></i></div>
      <small>${item.percent}%</small>
    </div>`).join("");
}

function containerMini(items) {
  if (!items.length) return `<div class="empty-state">Keine Container vorhanden</div>`;
  return items.slice(0, 6).map(item => `
    <div class="container-mini">
      <div><strong>${escapeHtml(item.name)}</strong><i class="state-dot ${item.state}"></i></div>
      <small>${escapeHtml(item.image)}</small>
    </div>`).join("");
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
        <td><div class="container-name"><span class="cube">⬡</span><div><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.image)}</small></div></div></td>
        <td><span class="state ${item.state}"><i class="state-dot ${item.state}"></i>${escapeHtml(item.state)}</span><br><small>${escapeHtml(item.status)}</small></td>
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
  $("#version").textContent = `Version ${data.version} · latest`;
  $("#version-badge").textContent = `v${data.version} · latest`;
  $("#distro-name").textContent = system.distro.name.toUpperCase();
  $("#distro-logo").src = `https://cdn.simpleicons.org/${encodeURIComponent(system.distro.icon)}/${system.distro.color.replace("#", "")}`;
  document.documentElement.style.setProperty("--brand", system.distro.color);
  if (!sshSocket || sshSocket.readyState !== WebSocket.OPEN) {
    $("#terminal-title").textContent = `${system.distro.id}-control@${system.hostname}:~`;
  }
  $("#hostname").textContent = system.hostname;
  $("#os").textContent = system.os;
  $("#kernel").textContent = system.kernel;
  $("#uptime").textContent = duration(system.uptime);
  $("#load").textContent = system.load.join(" · ");
  $("#cpu-value").textContent = `${system.cpu.percent}%`;
  $("#cpu-sub").textContent = `${system.cpu.cores} ${t("common.cores")} · ${system.cpu.model}`;
  $("#cpu-ring-value").textContent = Math.round(system.cpu.percent);
  $("#cpu-ring").style.setProperty("--value", system.cpu.percent);
  $("#ram-value").textContent = `${system.memory.percent}%`;
  $("#ram-sub").textContent = `${bytes(system.memory.used)} ${t("common.of")} ${bytes(system.memory.total)}`;
  $("#ram-ring-value").textContent = Math.round(system.memory.percent);
  $("#ram-ring").style.setProperty("--value", system.memory.percent);
  $("#network-value").textContent = bytes(system.network.down + system.network.up, true);
  $("#network-sub").textContent = `↓ ${bytes(system.network.down, true)} · ↑ ${bytes(system.network.up, true)}`;
  $("#docker-value").textContent = docker.available ? `${docker.containersRunning} ${t("common.active")}` : "Offline";
  $("#docker-sub").textContent = docker.available ? `${docker.containers.length} ${t("common.containers")} · Docker ${docker.version}` : docker.error;
  $("#architecture").textContent = system.architecture;
  $("#cores").textContent = system.cpu.cores;
  $("#interfaces").textContent = system.network.interfaces.join(", ") || "–";
  $("#dashboard-uptime").textContent = duration(data.dashboardUptime);
  $("#container-preview").classList.remove("skeleton-block");
  $("#container-preview").innerHTML = docker.available ? containerMini(docker.containers) : `<div class="empty-state">${escapeHtml(docker.error)}</div>`;
  $("#storage-preview").classList.remove("skeleton-block");
  $("#storage-preview").innerHTML = storageRows(data.storage, 3);
  $("#temperatures").innerHTML = system.temperatures.length
    ? system.temperatures.map(temp => `<div class="temp-row"><span>${escapeHtml(temp.name)}</span><strong>${temp.value} °C</strong></div>`).join("")
    : `<span>${t("common.noSensors")}</span>`;
  $("#temperatures").classList.toggle("empty-state", !system.temperatures.length);
  $("#storage-cards").innerHTML = data.storage.length ? data.storage.map(item => `
    <article class="storage-card">
      <div class="storage-card-head"><div><h3>${escapeHtml(item.mount)}</h3><p>${escapeHtml(item.device)} · ${escapeHtml(item.filesystem)}</p></div><strong>${item.percent}%</strong></div>
      <div class="big-bar"><i style="width:${Math.min(item.percent, 100)}%"></i></div>
      <div class="storage-stats"><span>${bytes(item.used)} belegt</span><span>${bytes(item.available)} frei · ${bytes(item.total)} gesamt</span></div>
    </article>`).join("") : `<div class="error-box">${t("common.noDrives")}</div>`;
  renderContainers(docker);
  $("#updated").textContent = `${t("live.synced")} ${new Date().toLocaleTimeString(currentLanguage, {hour: "2-digit", minute: "2-digit", second: "2-digit"})}`;
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
    $(".live-status").classList.remove("offline");
    $("#live-status").textContent = `LIVE · ${LIVE_INTERVAL_MS} ms`;
    if (manual) toast("Daten wurden aktualisiert");
  } catch (error) {
    failedUpdates++;
    toast(`Verbindung fehlgeschlagen: ${error.message}`, true);
    $("#updated").textContent = "Keine Verbindung";
    $(".live-status").classList.add("offline");
    $("#live-status").textContent = t("live.disconnected");
  } finally {
    busy = false;
    $("#refresh").classList.remove("spinning");
    scheduleLiveUpdate(document.hidden ? 5000 : failedUpdates ? 3000 : LIVE_INTERVAL_MS);
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
  return `/api/shares?${params}`;
}

async function loadShares(share = null, path = "") {
  try {
    const response = await fetch(shareUrl(share, path), {cache: "no-store"});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Freigaben konnten nicht geladen werden");
    selectedShare = data.selected ?? share;
    selectedPath = data.relative || "";
    $("#share-list").classList.remove("loading");
    $("#share-list").innerHTML = data.shares.length ? data.shares.map(item => `
      <button class="share-button ${Number(selectedShare) === item.id ? "active" : ""}" data-share="${item.id}">
        <span class="share-folder">▰</span>
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
        <span class="file-icon ${item.type === "directory" ? "folder" : ""}">${item.type === "directory" ? "▰" : "▤"}</span>
        ${item.type === "directory"
          ? `<button data-open-path="${escapeHtml(nextPath)}">${escapeHtml(item.name)}</button>`
          : `<span>${escapeHtml(item.name)}</span>`}
      </div>
      <span class="file-meta">${item.type === "directory" ? t("common.folder") : bytes(item.size)}</span>
      <span class="file-meta modified">${new Date(item.modified * 1000).toLocaleString("de-DE", {dateStyle: "short", timeStyle: "short"})}</span>
    </div>`;
  }).join("") : `<div class="empty-state">${t("common.empty")}</div>`;
  $$("[data-open-path]").forEach(button => button.addEventListener("click", () => loadShares(selectedShare, button.dataset.openPath)));
}

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
      sshTerminal.write(new Uint8Array(event.data));
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
    const response = await fetch("/api/account");
    if (!response.ok) throw new Error("Account could not be loaded.");
    const account = await response.json();
    $("#account-username").value = account.username || "";
    $("#settings-account-name").textContent = account.username || "Account";
    $("#settings-avatar").textContent = (account.username || "A").charAt(0).toUpperCase();
  } catch (error) {
    $("#account-error").textContent = error.message;
  }
}

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
    loadOverview();
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
