const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
let overview = null;
let busy = false;

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
  return days ? `${days} T ${hours} Std` : hours ? `${hours} Std ${mins} Min` : `${mins} Min`;
};

const toast = (message, error = false) => {
  const element = $("#toast");
  element.textContent = message;
  element.className = error ? "show error" : "show";
  clearTimeout(element.timer);
  element.timer = setTimeout(() => element.className = "", 2800);
};

function setPage(name) {
  $$(".page").forEach(page => page.classList.toggle("active", page.id === `page-${name}`));
  $$(".nav").forEach(nav => nav.classList.toggle("active", nav.dataset.page === name));
  const titles = {
    overview: ["SYSTEMZENTRALE", "Übersicht"],
    docker: ["WORKLOADS", "Docker"],
    storage: ["DATEISYSTEME", "Speicher"],
    processes: ["HOST", "Prozesse"],
    logs: ["EREIGNISSE", "Systemlogs"],
  };
  $("#page-kicker").textContent = titles[name][0];
  $("#page-title").textContent = titles[name][1];
  document.body.classList.remove("menu-open");
  if (name === "processes") loadProcesses();
  if (name === "logs") loadLogs();
}

$$(".nav").forEach(button => button.addEventListener("click", () => setPage(button.dataset.page)));
$$("[data-jump]").forEach(button => button.addEventListener("click", () => setPage(button.dataset.jump)));
$(".mobile-menu").addEventListener("click", () => document.body.classList.toggle("menu-open"));
$("#refresh").addEventListener("click", () => loadOverview(true));

function storageRows(items, limit = items.length) {
  if (!items.length) return `<div class="empty-state">Keine Laufwerke erkannt</div>`;
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
  $("#version").textContent = `Version ${data.version}`;
  $("#hostname").textContent = system.hostname;
  $("#os").textContent = system.os;
  $("#kernel").textContent = system.kernel;
  $("#uptime").textContent = duration(system.uptime);
  $("#load").textContent = system.load.join(" · ");
  $("#cpu-value").textContent = `${system.cpu.percent}%`;
  $("#cpu-sub").textContent = `${system.cpu.cores} Kerne · ${system.cpu.model}`;
  $("#cpu-ring-value").textContent = Math.round(system.cpu.percent);
  $("#cpu-ring").style.setProperty("--value", system.cpu.percent);
  $("#ram-value").textContent = `${system.memory.percent}%`;
  $("#ram-sub").textContent = `${bytes(system.memory.used)} von ${bytes(system.memory.total)}`;
  $("#ram-ring-value").textContent = Math.round(system.memory.percent);
  $("#ram-ring").style.setProperty("--value", system.memory.percent);
  $("#network-value").textContent = bytes(system.network.down + system.network.up, true);
  $("#network-sub").textContent = `↓ ${bytes(system.network.down, true)} · ↑ ${bytes(system.network.up, true)}`;
  $("#docker-value").textContent = docker.available ? `${docker.containersRunning} aktiv` : "Offline";
  $("#docker-sub").textContent = docker.available ? `${docker.containers.length} Container · Docker ${docker.version}` : docker.error;
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
    : `<span>Keine Sensorwerte verfügbar</span>`;
  $("#temperatures").classList.toggle("empty-state", !system.temperatures.length);
  $("#storage-cards").innerHTML = data.storage.length ? data.storage.map(item => `
    <article class="storage-card">
      <div class="storage-card-head"><div><h3>${escapeHtml(item.mount)}</h3><p>${escapeHtml(item.device)} · ${escapeHtml(item.filesystem)}</p></div><strong>${item.percent}%</strong></div>
      <div class="big-bar"><i style="width:${Math.min(item.percent, 100)}%"></i></div>
      <div class="storage-stats"><span>${bytes(item.used)} belegt</span><span>${bytes(item.available)} frei · ${bytes(item.total)} gesamt</span></div>
    </article>`).join("") : `<div class="error-box">Keine Laufwerke erkannt.</div>`;
  renderContainers(docker);
  $("#updated").textContent = `Aktualisiert ${new Date().toLocaleTimeString("de-DE", {hour: "2-digit", minute: "2-digit", second: "2-digit"})}`;
}

async function loadOverview(manual = false) {
  if (busy) return;
  busy = true;
  $("#refresh").classList.add("spinning");
  try {
    const response = await fetch("/api/overview");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
    if (manual) toast("Daten wurden aktualisiert");
  } catch (error) {
    toast(`Verbindung fehlgeschlagen: ${error.message}`, true);
    $("#updated").textContent = "Keine Verbindung";
  } finally {
    busy = false;
    $("#refresh").classList.remove("spinning");
  }
}

async function dockerAction(button) {
  const actionNames = {start: "starten", stop: "stoppen", restart: "neu starten"};
  const action = button.dataset.action;
  if ((action === "stop" || action === "restart") &&
      !window.confirm(`Container wirklich ${actionNames[action]}?`)) return;
  button.disabled = true;
  button.textContent = "…";
  try {
    const response = await fetch(`/api/docker/${button.dataset.id}/${action}`, {method: "POST"});
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

loadOverview();
setInterval(loadOverview, 5000);
