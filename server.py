#!/usr/bin/env python3
import base64
import json
import os
import platform
import re
import socket
import subprocess
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent
HOST_ROOT = Path(os.getenv("HOST_ROOT", "/host"))
DOCKER_SOCKET = os.getenv("DOCKER_SOCKET", "/var/run/docker.sock")
VERSION = os.getenv("APP_VERSION", "1.2.0")
APP_USER = os.getenv("DASHBOARD_USER", "")
APP_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
ALLOW_ACTIONS = os.getenv("ALLOW_DOCKER_ACTIONS", "true").lower() == "true"
STARTED = time.time()
_sample = {"at": 0, "cpu": None, "net": None}
_cache = {}


def read_text(path, default=""):
    try:
        return Path(path).read_text(errors="replace")
    except (OSError, PermissionError):
        return default


def host_path(path):
    return HOST_ROOT / path.lstrip("/")


def cached(key, ttl, loader):
    now = time.time()
    stored = _cache.get(key)
    if stored and now - stored[0] < ttl:
        return stored[1]
    value = loader()
    _cache[key] = (now, value)
    return value


def bytes_label(value):
    value = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1024 or unit == "PB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


def cpu_snapshot():
    line = read_text(host_path("/proc/stat")).splitlines()
    if not line:
        return None
    values = [int(v) for v in line[0].split()[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def net_snapshot():
    result = {}
    for line in read_text(host_path("/proc/net/dev")).splitlines()[2:]:
        if ":" not in line:
            continue
        name, values = line.split(":", 1)
        fields = values.split()
        name = name.strip()
        if name != "lo" and len(fields) >= 9:
            result[name] = (int(fields[0]), int(fields[8]))
    return result


def memory_info():
    values = {}
    for line in read_text(host_path("/proc/meminfo")).splitlines():
        if ":" in line:
            key, raw = line.split(":", 1)
            values[key] = int(raw.strip().split()[0]) * 1024
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    swap_total = values.get("SwapTotal", 0)
    swap_free = values.get("SwapFree", 0)
    return {
        "total": total,
        "used": max(0, total - available),
        "percent": round((total - available) / total * 100, 1) if total else 0,
        "swapTotal": swap_total,
        "swapUsed": max(0, swap_total - swap_free),
    }


def temperatures():
    found = []
    base = host_path("/sys/class/thermal")
    try:
        for zone in base.glob("thermal_zone*"):
            raw = read_text(zone / "temp").strip()
            if raw and raw.lstrip("-").isdigit():
                value = int(raw) / 1000
                if -10 < value < 150:
                    found.append({
                        "name": read_text(zone / "type", zone.name).strip(),
                        "value": round(value, 1),
                    })
    except OSError:
        pass
    return found[:8]


def system_info():
    global _sample
    now = time.time()
    current_cpu = cpu_snapshot()
    current_net = net_snapshot()
    cpu_percent = 0
    rates = {"down": 0, "up": 0}
    elapsed = now - _sample["at"]
    if _sample["cpu"] and current_cpu and elapsed > 0:
        total_delta = current_cpu[0] - _sample["cpu"][0]
        idle_delta = current_cpu[1] - _sample["cpu"][1]
        if total_delta > 0:
            cpu_percent = round((total_delta - idle_delta) / total_delta * 100, 1)
    if _sample["net"] and elapsed > 0:
        rates["down"] = max(0, sum(v[0] for v in current_net.values()) -
                            sum(v[0] for v in _sample["net"].values())) / elapsed
        rates["up"] = max(0, sum(v[1] for v in current_net.values()) -
                          sum(v[1] for v in _sample["net"].values())) / elapsed
    _sample = {"at": now, "cpu": current_cpu, "net": current_net}

    uptime_raw = read_text(host_path("/proc/uptime"), "0").split()
    uptime = int(float(uptime_raw[0])) if uptime_raw else 0
    load = read_text(host_path("/proc/loadavg"), "0 0 0").split()[:3]
    os_release = {}
    for line in read_text(host_path("/etc/os-release")).splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            os_release[key] = value.strip().strip('"')
    distro_id = os_release.get("ID", "linux").lower()
    distro_icons = {
        "ubuntu": ("ubuntu", "#E95420"),
        "debian": ("debian", "#A81D33"),
        "fedora": ("fedora", "#51A2DA"),
        "arch": ("archlinux", "#1793D1"),
        "manjaro": ("manjaro", "#35BF5C"),
        "linuxmint": ("linuxmint", "#86BE43"),
        "opensuse": ("opensuse", "#73BA25"),
        "opensuse-tumbleweed": ("opensuse", "#73BA25"),
        "alpine": ("alpinelinux", "#0D597F"),
        "rocky": ("rockylinux", "#10B981"),
    }
    icon, color = distro_icons.get(distro_id, ("linux", "#FCC624"))
    return {
        "hostname": read_text(host_path("/etc/hostname"), socket.gethostname()).strip(),
        "os": os_release.get("PRETTY_NAME", "Ubuntu Server"),
        "distro": {
            "id": distro_id,
            "name": os_release.get("NAME", distro_id.title()),
            "icon": icon,
            "color": color,
        },
        "kernel": read_text(host_path("/proc/sys/kernel/osrelease"), platform.release()).strip(),
        "architecture": platform.machine(),
        "uptime": uptime,
        "load": load,
        "cpu": {
            "percent": cpu_percent,
            "model": next((line.split(":", 1)[1].strip() for line in
                           read_text(host_path("/proc/cpuinfo")).splitlines()
                           if line.lower().startswith("model name")), "CPU"),
            "cores": max(1, len(re.findall(r"^processor\s*:", read_text(host_path("/proc/cpuinfo")), re.M))),
        },
        "memory": memory_info(),
        "network": {
            "down": round(rates["down"]),
            "up": round(rates["up"]),
            "interfaces": sorted(current_net.keys()),
        },
        "temperatures": temperatures(),
    }


def docker_request(method, path, body=None):
    if not Path(DOCKER_SOCKET).exists():
        raise RuntimeError("Docker-Socket ist nicht eingebunden")
    payload = json.dumps(body).encode() if body is not None else b""
    request = (
        f"{method} {path} HTTP/1.1\r\n"
        "Host: docker\r\n"
        "Connection: close\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Content-Type: application/json\r\n\r\n"
    ).encode() + payload
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(8)
    try:
        client.connect(DOCKER_SOCKET)
        client.sendall(request)
        chunks = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        client.close()
    raw = b"".join(chunks)
    header, _, data = raw.partition(b"\r\n\r\n")
    status = int(header.split(b" ", 2)[1])
    if b"transfer-encoding: chunked" in header.lower():
        decoded = b""
        while data:
            line, _, rest = data.partition(b"\r\n")
            size = int(line.split(b";")[0], 16)
            if size == 0:
                break
            decoded += rest[:size]
            data = rest[size + 2:]
        data = decoded
    parsed = json.loads(data) if data.strip() else None
    if status >= 400:
        message = parsed.get("message", "Docker-Fehler") if isinstance(parsed, dict) else "Docker-Fehler"
        raise RuntimeError(message)
    return parsed


def docker_info():
    try:
        info = docker_request("GET", "/info")
        raw = docker_request("GET", "/containers/json?all=1") or []
        containers = []
        own_container_id = socket.gethostname()
        for item in raw:
            state = item.get("State", "unknown")
            containers.append({
                "id": item.get("Id", "")[:12],
                "fullId": item.get("Id", ""),
                "name": (item.get("Names") or ["Unbenannt"])[0].lstrip("/"),
                "image": item.get("Image", ""),
                "state": state,
                "status": item.get("Status", ""),
                "created": item.get("Created", 0),
                "isSelf": item.get("Id", "").startswith(own_container_id),
                "ports": [
                    f"{p.get('PublicPort', p.get('PrivatePort'))}:{p.get('PrivatePort')}/{p.get('Type', 'tcp')}"
                    for p in item.get("Ports", []) if p.get("PrivatePort")
                ],
            })
        order = {"running": 0, "restarting": 1, "paused": 2, "exited": 3, "dead": 4}
        containers.sort(key=lambda c: (order.get(c["state"], 9), c["name"].lower()))
        return {
            "available": True,
            "version": info.get("ServerVersion", ""),
            "containersRunning": info.get("ContainersRunning", 0),
            "containersStopped": info.get("ContainersStopped", 0),
            "images": info.get("Images", 0),
            "containers": containers,
        }
    except Exception as exc:
        return {"available": False, "error": str(exc), "containers": []}


def storage_info():
    ignored = ("proc", "sysfs", "tmpfs", "devtmpfs", "cgroup", "overlay", "squashfs",
               "nsfs", "tracefs", "securityfs", "pstore", "debugfs", "mqueue", "fusectl")
    useful_prefixes = ("/mnt", "/media", "/srv", "/storage", "/data", "/boot", "/home")
    network_filesystems = ("nfs", "nfs4", "cifs")
    result, seen = [], set()
    for line in read_text(host_path("/proc/mounts")).splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        device, mount, fs = parts[:3]
        mount = mount.replace("\\040", " ")
        if fs.startswith(ignored) or mount in seen or not mount.startswith("/"):
            continue
        if not (device.startswith("/dev/") or fs in ("zfs", "btrfs", *network_filesystems)):
            continue
        # Container-Runtimes und Desktop-Sandboxes erzeugen zahlreiche Datei-
        # Bind-Mounts unter /etc und /app. Für ein Server-Dashboard sind nur
        # echte System-, Daten- und Netzwerk-Mounts relevant.
        if mount != "/" and not mount.startswith(useful_prefixes) and fs not in network_filesystems:
            continue
        try:
            stats = os.statvfs(host_path(mount))
            total = stats.f_blocks * stats.f_frsize
            available = stats.f_bavail * stats.f_frsize
            used = max(0, total - stats.f_bfree * stats.f_frsize)
            result.append({
                "device": device,
                "mount": mount,
                "filesystem": fs,
                "total": total,
                "used": used,
                "available": available,
                "percent": round(used / total * 100, 1) if total else 0,
            })
            seen.add(mount)
        except OSError:
            pass
    result.sort(key=lambda d: (d["mount"] != "/", d["mount"]))
    return result


def process_info():
    processes = []
    proc = host_path("/proc")
    try:
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            status = read_text(entry / "status")
            values = {}
            for line in status.splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    values[key] = value.strip()
            rss = int(values.get("VmRSS", "0 kB").split()[0]) * 1024
            processes.append({
                "pid": int(entry.name),
                "name": values.get("Name", "?"),
                "state": values.get("State", "?"),
                "memory": rss,
                "user": values.get("Uid", "?").split()[0],
            })
    except OSError:
        pass
    return sorted(processes, key=lambda p: p["memory"], reverse=True)[:25]


def log_info():
    candidates = ["/var/log/syslog", "/var/log/messages", "/var/log/kern.log"]
    for candidate in candidates:
        path = host_path(candidate)
        if path.is_file():
            try:
                lines = path.read_text(errors="replace").splitlines()
                return {"source": candidate, "lines": lines[-120:]}
            except OSError:
                continue
    return {"source": "Dashboard", "lines": [
        "Keine klassische Host-Logdatei gefunden.",
        "Bei journald-only Systemen stehen Logs im Terminal über journalctl zur Verfügung."
    ]}


def share_roots():
    roots = []
    seen = set()

    def add(name, path, protocol="Lokal"):
        normalized = "/" + str(path).strip().lstrip("/")
        actual = host_path(normalized)
        try:
            resolved = actual.resolve()
            host_resolved = HOST_ROOT.resolve()
            if not resolved.is_dir() or (resolved != host_resolved and host_resolved not in resolved.parents):
                return
        except OSError:
            return
        key = str(resolved)
        if key not in seen:
            roots.append({"name": name, "path": normalized, "protocol": protocol, "actual": resolved})
            seen.add(key)

    smb = read_text(host_path("/etc/samba/smb.conf"))
    current = None
    sections = {}
    for raw in smb.splitlines():
        line = raw.strip()
        section = re.fullmatch(r"\[([^\]]+)\]", line)
        if section:
            current = section.group(1)
            sections[current] = {}
        elif current and "=" in line and not line.startswith(("#", ";")):
            key, value = line.split("=", 1)
            sections[current][key.strip().lower()] = value.strip()
    for name, options in sections.items():
        if name.lower() not in ("global", "homes", "printers", "print$") and options.get("path"):
            add(name, options["path"], "SMB")

    configured = [item.strip() for item in os.getenv("SHARE_ROOTS", "").split(",") if item.strip()]
    for path in configured:
        add(Path(path).name or path, path, "Konfiguriert")

    if not roots:
        for base_path in ("/mnt", "/srv", "/media"):
            base = host_path(base_path)
            try:
                children = sorted((item for item in base.iterdir() if item.is_dir()), key=lambda p: p.name.lower())
                if children:
                    for child in children[:30]:
                        add(child.name, f"{base_path}/{child.name}")
                elif base.is_dir():
                    add(Path(base_path).name, base_path)
            except OSError:
                pass
    return roots


def shares_info(share_index=None, relative=""):
    roots = share_roots()
    public_roots = []
    for index, root in enumerate(roots):
        try:
            stats = os.statvfs(root["actual"])
            total = stats.f_blocks * stats.f_frsize
            free = stats.f_bavail * stats.f_frsize
        except OSError:
            total = free = 0
        public_roots.append({
            "id": index,
            "name": root["name"],
            "path": root["path"],
            "protocol": root["protocol"],
            "total": total,
            "free": free,
        })
    if share_index is None:
        return {"shares": public_roots, "entries": [], "relative": ""}
    try:
        root = roots[int(share_index)]
    except (ValueError, IndexError, TypeError):
        raise ValueError("Unbekannte Freigabe")
    relative = unquote(relative).strip("/")
    target = (root["actual"] / relative).resolve()
    if target != root["actual"] and root["actual"] not in target.parents:
        raise ValueError("Pfad außerhalb der Freigabe")
    if not target.is_dir():
        raise ValueError("Ordner nicht gefunden")
    entries = []
    try:
        items = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        for item in items[:500]:
            try:
                stat_info = item.stat()
                entries.append({
                    "name": item.name,
                    "type": "directory" if item.is_dir() else "file",
                    "size": stat_info.st_size,
                    "modified": int(stat_info.st_mtime),
                    "hidden": item.name.startswith("."),
                })
            except OSError:
                pass
    except OSError as exc:
        raise ValueError(str(exc))
    return {
        "shares": public_roots,
        "selected": int(share_index),
        "relative": relative,
        "entries": entries,
        "truncated": len(entries) == 500,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = f"UbuntuDashboard/{VERSION}"

    def log_message(self, fmt, *args):
        print(f"[web] {self.address_string()} {fmt % args}")

    def authenticated(self):
        if not APP_USER or not APP_PASSWORD:
            return True
        header = self.headers.get("Authorization", "")
        try:
            value = base64.b64decode(header.removeprefix("Basic ")).decode()
            return value == f"{APP_USER}:{APP_PASSWORD}"
        except Exception:
            return False

    def send_json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self.authenticated():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Ubuntu Dashboard"')
            self.end_headers()
            return
        path = urlparse(self.path).path
        if path == "/api/overview":
            self.send_json({
                "version": VERSION,
                "dashboardUptime": int(time.time() - STARTED),
                "system": system_info(),
                "docker": cached("docker", 1.5, docker_info),
                "storage": cached("storage", 8, storage_info),
            })
        elif path == "/api/processes":
            self.send_json({"processes": process_info()})
        elif path == "/api/logs":
            self.send_json(log_info())
        elif path == "/api/shares":
            query = parse_qs(urlparse(self.path).query)
            share = query.get("share", [None])[0]
            relative = query.get("path", [""])[0]
            try:
                self.send_json(shares_info(share, relative))
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
        elif path == "/api/health":
            self.send_json({"status": "ok", "version": VERSION})
        elif path.startswith("/api/"):
            self.send_json({"error": "Nicht gefunden"}, 404)
        else:
            self.serve_static(path)

    def do_POST(self):
        if not self.authenticated():
            self.send_json({"error": "Nicht angemeldet"}, 401)
            return
        path = urlparse(self.path).path
        match = re.fullmatch(r"/api/docker/([a-f0-9]{12,64})/(start|stop|restart)", path)
        if not match:
            self.send_json({"error": "Ungültige Aktion"}, 404)
            return
        if not ALLOW_ACTIONS:
            self.send_json({"error": "Docker-Aktionen sind deaktiviert"}, 403)
            return
        container, action = match.groups()
        try:
            docker_request("POST", f"/containers/{container}/{action}?t=10")
            _cache.pop("docker", None)
            self.send_json({"ok": True, "action": action})
        except Exception as exc:
            self.send_json({"error": str(exc)}, 409)

    def serve_static(self, path):
        relative = "index.html" if path in ("", "/") else unquote(path).lstrip("/")
        target = (ROOT / "web" / relative).resolve()
        web_root = (ROOT / "web").resolve()
        if web_root not in target.parents and target != web_root:
            self.send_error(403)
            return
        if not target.is_file():
            target = web_root / "index.html"
        mime = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
        }.get(target.suffix, "application/octet-stream")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache" if target.suffix == ".html" else "public, max-age=3600")
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    print(f"Ubuntu Dashboard {VERSION} läuft auf 0.0.0.0:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
