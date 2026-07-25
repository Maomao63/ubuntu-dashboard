#!/usr/bin/env python3
import base64
import concurrent.futures
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
import platform
import pty
import re
import secrets
import select
import signal
import shutil
import socket
import stat
import struct
import subprocess
import termios
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

ROOT = Path(__file__).resolve().parent
HOST_ROOT = Path(os.getenv("HOST_ROOT", "/host"))
DOCKER_SOCKET = os.getenv("DOCKER_SOCKET", "/var/run/docker.sock")
# Deliberately image-owned: old Compose files must not be able to override the UI version.
VERSION = "1.16.0"
APP_USER = os.getenv("DASHBOARD_USER", "")
APP_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
CONFIG_FILE = Path(os.getenv("CONFIG_FILE", "/data/config.json"))
IFRAME_CONFIG_FILE = CONFIG_FILE.with_name("iframe.json")
LEGACY_ACCOUNT_FILE = Path(os.getenv("ACCOUNT_FILE", "/data/account.json"))
LEGACY_NOTIFICATION_FILE = Path(os.getenv("NOTIFICATION_FILE", "/data/notifications.json"))
ALLOW_ACTIONS = os.getenv("ALLOW_DOCKER_ACTIONS", "true").lower() == "true"
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
SESSION_TTL = int(os.getenv("SESSION_TTL", "43200"))
SSH_HOST = os.getenv("SSH_HOST", "auto")
SSH_PORT = int(os.getenv("SSH_PORT", "22"))
NETWORK_INTERFACE = os.getenv("NETWORK_INTERFACE", "auto").strip()
HOST_MOUNTS_FILE = Path(os.getenv("HOST_MOUNTS_FILE", "/host-proc-mounts"))
HOST_NET_ROUTE_FILE = Path(os.getenv("HOST_NET_ROUTE_FILE", "/host-proc-net-route"))
STARTED = time.time()
_sample = {"at": 0, "cpu": None, "net": None}
_sample_lock = threading.Lock()
_cache = {}
_sessions = {}
_login_attempts = {}
_account = None
_config_lock = threading.RLock()
_notification_lock = threading.Lock()
_notification_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
_notification_pending = False
_notification_state = {}
_notification_runtime = {"lastSent": None, "lastError": ""}

NOTIFICATION_DEFAULTS = {
    "enabled": False,
    "webhookUrl": "",
    "mention": "",
    "diskAlerts": True,
    "containerAlerts": True,
    "systemAlerts": True,
    "repeatMinutes": 60,
}


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


def new_session(username):
    now = time.time()
    for expired_token, stored in list(_sessions.items()):
        if stored["expires"] <= now:
            _sessions.pop(expired_token, None)
    token = secrets.token_urlsafe(36)
    session = {
        "username": username,
        "expires": now + SESSION_TTL,
        "csrf": secrets.token_urlsafe(24),
    }
    _sessions[token] = session
    return token, session


def cookie_value(header, name):
    for item in (header or "").split(";"):
        key, _, value = item.strip().partition("=")
        if key == name:
            return value
    return ""


def password_record(username, password):
    salt = secrets.token_bytes(24)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310000)
    return {
        "username": username,
        "salt": base64.b64encode(salt).decode(),
        "passwordHash": base64.b64encode(digest).decode(),
    }


def load_config():
    with _config_lock:
        try:
            stored = json.loads(CONFIG_FILE.read_text())
            return stored if isinstance(stored, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            return {}


def save_config_section(section, value):
    with _config_lock:
        config = load_config()
        config[section] = value
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = CONFIG_FILE.with_suffix(f"{CONFIG_FILE.suffix}.tmp")
        temporary.write_text(json.dumps(config, indent=2) + "\n")
        os.chmod(temporary, 0o600)
        temporary.replace(CONFIG_FILE)


def valid_account_record(stored):
    return (
        isinstance(stored, dict)
        and stored.get("username")
        and stored.get("salt")
        and stored.get("passwordHash")
    )


def save_account(account):
    save_config_section("account", account)


def load_account():
    stored = load_config().get("account")
    if valid_account_record(stored):
        return stored

    try:
        legacy = json.loads(LEGACY_ACCOUNT_FILE.read_text())
        if valid_account_record(legacy):
            save_account(legacy)
            print(f"[config] Migrated account from {LEGACY_ACCOUNT_FILE} to {CONFIG_FILE}")
            return legacy
    except (OSError, ValueError, json.JSONDecodeError):
        pass

    if APP_USER and APP_PASSWORD:
        account = password_record(APP_USER, APP_PASSWORD)
        try:
            save_account(account)
        except OSError as exc:
            print(f"[security] Account storage is not persistent: {exc}")
        return account
    return None


def save_notification_settings(settings):
    save_config_section("discord", settings)


def normalized_notification_settings(stored):
    settings = dict(NOTIFICATION_DEFAULTS)
    if isinstance(stored, dict):
        for key in settings:
            if key in stored:
                settings[key] = stored[key]
    return settings


def load_notification_settings():
    config = load_config()
    stored = config.get("discord")
    if isinstance(stored, dict):
        return normalized_notification_settings(stored)

    try:
        legacy = json.loads(LEGACY_NOTIFICATION_FILE.read_text())
        if isinstance(legacy, dict):
            settings = normalized_notification_settings(legacy)
            save_notification_settings(settings)
            print(f"[config] Migrated Discord settings from {LEGACY_NOTIFICATION_FILE} to {CONFIG_FILE}")
            return settings
    except (OSError, ValueError, json.JSONDecodeError):
        pass

    return normalized_notification_settings(None)


def save_iframe_settings(settings):
    with _config_lock:
        IFRAME_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = IFRAME_CONFIG_FILE.with_suffix(f"{IFRAME_CONFIG_FILE.suffix}.tmp")
        temporary.write_text(json.dumps(settings, indent=2) + "\n")
        os.chmod(temporary, 0o600)
        temporary.replace(IFRAME_CONFIG_FILE)


def normalized_iframe_settings(stored):
    stored = stored if isinstance(stored, dict) else {}
    raw_targets = stored.get("targets")
    if not isinstance(raw_targets, list):
        raw_targets = []
        if stored.get("url"):
            raw_targets.append({
                "id": "legacy",
                "name": "Dashboard",
                "url": stored.get("url", ""),
                "port": stored.get("port", ""),
            })
    targets = []
    used_ids = set()
    for index, raw in enumerate(raw_targets[:24]):
        if not isinstance(raw, dict):
            continue
        target_id = str(raw.get("id", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,64}", target_id) or target_id in used_ids:
            target_id = f"target-{index + 1}"
            while target_id in used_ids:
                target_id += "-x"
        used_ids.add(target_id)
        name = str(raw.get("name", "")).strip()[:48] or f"Dashboard {index + 1}"
        targets.append({
            "id": target_id,
            "name": name,
            "url": str(raw.get("url", "")).strip(),
            "port": raw.get("port", ""),
        })
    selected_id = str(stored.get("selectedId", "")).strip()
    if targets and selected_id not in used_ids:
        selected_id = targets[0]["id"]
    return {
        "enabled": stored.get("enabled") is True,
        "targets": targets,
        "selectedId": selected_id,
    }


def load_iframe_settings():
    try:
        return normalized_iframe_settings(json.loads(IFRAME_CONFIG_FILE.read_text()))
    except (OSError, ValueError, json.JSONDecodeError):
        return normalized_iframe_settings(None)


def iframe_source(settings):
    value = str(settings.get("url", "")).strip().rstrip("/")
    if not value:
        return ""
    if "://" not in value:
        value = f"http://{value}"
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("iFrame URL must be a valid http:// or https:// address without credentials.")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("Invalid port in iFrame URL.") from exc
    raw_port = settings.get("port", "")
    if raw_port not in ("", None):
        port = int(raw_port)
        if not 1 <= port <= 65535:
            raise ValueError("iFrame port must be between 1 and 65535.")
        hostname = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        parsed = parsed._replace(netloc=f"{hostname}:{port}")
    return parsed.geturl()


def public_iframe_settings():
    settings = load_iframe_settings()
    targets = []
    for target in settings["targets"]:
        try:
            source = iframe_source(target)
            error = ""
        except (ValueError, TypeError) as exc:
            source = ""
            error = str(exc)
        targets.append({**target, "src": source, "error": error})
    selected = next(
        (target for target in targets if target["id"] == settings["selectedId"]),
        targets[0] if targets else None,
    )
    return {
        "enabled": settings["enabled"],
        "targets": targets,
        "selectedId": selected["id"] if selected else "",
        "src": selected["src"] if selected else "",
        "error": selected["error"] if selected else "",
    }


def account_configured():
    return _account is not None


def verify_account(username, password):
    if not _account:
        return False
    try:
        salt = base64.b64decode(_account["salt"])
        expected = base64.b64decode(_account["passwordHash"])
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310000)
        return hmac.compare_digest(username, _account["username"]) and hmac.compare_digest(actual, expected)
    except (KeyError, ValueError):
        return False


_account = load_account()
_notification_settings = load_notification_settings()


def version_tuple(value):
    numbers = re.findall(r"\d+", str(value))[:3]
    return tuple(int(number) for number in numbers) + (0,) * (3 - len(numbers))


def github_version_info():
    request = urllib.request.Request(
        "https://raw.githubusercontent.com/Maomao63/ubuntu-dashboard/main/Dockerfile",
        headers={"User-Agent": f"ubuntu-dashboard/{VERSION}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            source = response.read(65536).decode(errors="replace")
        match = re.search(r"ARG APP_VERSION=([0-9][0-9.]*)", source)
        latest = match.group(1) if match else VERSION
        return {
            "current": VERSION,
            "latest": latest,
            "updateAvailable": version_tuple(latest) > version_tuple(VERSION),
            "checked": True,
        }
    except Exception as exc:
        return {
            "current": VERSION,
            "latest": None,
            "updateAvailable": False,
            "checked": False,
            "error": str(exc),
        }


def request_host(handler):
    raw = handler.headers.get("Host", "")
    try:
        return urlparse(f"//{raw}").hostname or raw.split(":")[0]
    except ValueError:
        return raw.split(":")[0]


def ssh_target(handler):
    return request_host(handler) if SSH_HOST.lower() == "auto" else SSH_HOST


def recv_exact(sock, length):
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            return b""
        data += chunk
    return data


def websocket_read(sock):
    header = recv_exact(sock, 2)
    if len(header) < 2:
        return None, b""
    first, second = header
    opcode = first & 0x0F
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", recv_exact(sock, 8))[0]
    mask = recv_exact(sock, 4) if second & 0x80 else b""
    data = recv_exact(sock, length)
    if mask:
        data = bytes(value ^ mask[index % 4] for index, value in enumerate(data))
    return opcode, data


def websocket_send(sock, data, opcode=1):
    if isinstance(data, str):
        data = data.encode()
    length = len(data)
    header = bytes([0x80 | opcode])
    if length < 126:
        header += bytes([length])
    elif length < 65536:
        header += bytes([126]) + struct.pack("!H", length)
    else:
        header += bytes([127]) + struct.pack("!Q", length)
    sock.sendall(header + data)


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


def monitored_interfaces(available):
    configured = [name.strip() for name in NETWORK_INTERFACE.split(",") if name.strip()]
    if configured and configured != ["auto"]:
        return [name for name in configured if name in available]
    default_routes = []
    route_text = ""
    for source in (HOST_NET_ROUTE_FILE, host_path("/proc/1/net/route"), host_path("/proc/net/route")):
        route_text = read_text(source)
        if route_text.strip():
            break
    for line in route_text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 8 or fields[1] != "00000000":
            continue
        try:
            flags, metric = int(fields[3], 16), int(fields[6])
        except ValueError:
            continue
        if flags & 0x2 and fields[0] in available:
            default_routes.append((metric, fields[0]))
    if default_routes:
        best_metric = min(item[0] for item in default_routes)
        return sorted({name for metric, name in default_routes if metric == best_metric})
    physical = [
        name for name in available
        if host_path(f"/sys/class/net/{name}/device").exists()
        and read_text(host_path(f"/sys/class/net/{name}/operstate"), "up").strip() in ("up", "unknown")
    ]
    return physical or [name for name in available if name != "lo"]


def net_snapshot():
    available = {}
    sysfs = host_path("/sys/class/net")
    try:
        interfaces = list(sysfs.iterdir())
    except OSError:
        interfaces = []
    for interface in interfaces:
        if interface.name == "lo":
            continue
        try:
            received = int(read_text(interface / "statistics/rx_bytes").strip())
            transmitted = int(read_text(interface / "statistics/tx_bytes").strip())
            available[interface.name] = (received, transmitted)
        except ValueError:
            continue
    if not available:
        for source in (host_path("/proc/1/net/dev"), host_path("/proc/net/dev")):
            content = read_text(source)
            if not content.strip():
                continue
            for line in content.splitlines()[2:]:
                if ":" not in line:
                    continue
                name, values = line.split(":", 1)
                fields = values.split()
                name = name.strip()
                if name != "lo" and len(fields) >= 9:
                    available[name] = (int(fields[0]), int(fields[8]))
            if available:
                break
    selected = monitored_interfaces(available)
    return {name: available[name] for name in selected}


def network_rates(current, previous, elapsed):
    if not previous or elapsed <= 0:
        return {"down": 0, "up": 0}
    shared = current.keys() & previous.keys()
    return {
        "down": sum(max(0, current[name][0] - previous[name][0]) for name in shared) / elapsed,
        "up": sum(max(0, current[name][1] - previous[name][1]) for name in shared) / elapsed,
    }


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


def os_release_info():
    values = {}
    for line in read_text(host_path("/etc/os-release")).splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')
    return values


def system_info():
    global _sample
    with _sample_lock:
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
            rates = network_rates(current_net, _sample["net"], elapsed)
        _sample = {"at": now, "cpu": current_cpu, "net": current_net}

    uptime_raw = read_text(host_path("/proc/uptime"), "0").split()
    uptime = int(float(uptime_raw[0])) if uptime_raw else 0
    load = read_text(host_path("/proc/loadavg"), "0 0 0").split()[:3]
    os_release = os_release_info()
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
    try:
        process_count = sum(1 for entry in host_path("/proc").iterdir() if entry.name.isdigit())
    except OSError:
        process_count = 0
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
        "processCount": process_count,
        "rootFilesystem": filesystem_usage("/"),
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
        "disks": cached("disk-telemetry", 10, disk_telemetry),
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
            status = item.get("Status", "")
            labels = item.get("Labels") or {}
            status_lower = status.lower()
            if "unhealthy" in status_lower:
                health = "unhealthy"
            elif "health: starting" in status_lower or state in ("restarting", "paused"):
                health = "starting"
            elif "healthy" in status_lower:
                health = "healthy"
            elif state == "running":
                health = "running"
            elif state in ("exited", "dead"):
                health = "stopped"
            else:
                health = "unknown"
            containers.append({
                "id": item.get("Id", "")[:12],
                "fullId": item.get("Id", ""),
                "name": (item.get("Names") or ["Unbenannt"])[0].lstrip("/"),
                "image": item.get("Image", ""),
                "imageId": item.get("ImageID", ""),
                "state": state,
                "health": health,
                "status": status,
                "created": item.get("Created", 0),
                "isSelf": item.get("Id", "").startswith(own_container_id),
                "stack": labels.get("com.docker.compose.project") or labels.get("com.docker.stack.namespace"),
                "service": labels.get("com.docker.compose.service") or labels.get("com.docker.swarm.service.name"),
                "ports": [
                    f"{p.get('PublicPort', p.get('PrivatePort'))}:{p.get('PrivatePort')}/{p.get('Type', 'tcp')}"
                    for p in item.get("Ports", []) if p.get("PrivatePort")
                ],
            })
        order = {"running": 0, "restarting": 1, "paused": 2, "exited": 3, "dead": 4}
        containers.sort(key=lambda c: (order.get(c["state"], 9), c["name"].lower()))
        stack_groups = {}
        for container in containers:
            if container["stack"]:
                stack_groups.setdefault(container["stack"], []).append(container)
        stacks = []
        for name, members in stack_groups.items():
            running = sum(member["state"] == "running" for member in members)
            if running == 0 or any(
                member["health"] == "unhealthy" or member["state"] == "dead"
                for member in members
            ):
                health = "critical"
            elif running < len(members) or any(
                member["health"] == "starting" or member["state"] in ("restarting", "paused")
                for member in members
            ):
                health = "warning"
            else:
                health = "healthy"
            stacks.append({
                "name": name,
                "running": running,
                "total": len(members),
                "health": health,
                "containerIds": [member["fullId"] for member in members],
                "problems": [
                    {"name": member["name"], "status": member["status"] or member["state"]}
                    for member in members
                    if member["state"] != "running" or member["health"] in ("unhealthy", "starting")
                ][:8],
            })
        health_order = {"critical": 0, "warning": 1, "healthy": 2}
        stacks.sort(key=lambda stack: (health_order[stack["health"]], stack["name"].lower()))
        aggregate_health = "unhealthy" if any(
            stack["health"] == "critical" for stack in stacks
        ) else "starting" if any(
            stack["health"] == "warning" for stack in stacks
        ) else "healthy"
        return {
            "available": True,
            "version": info.get("ServerVersion", ""),
            "containersRunning": info.get("ContainersRunning", 0),
            "containersStopped": info.get("ContainersStopped", 0),
            "images": info.get("Images", 0),
            "health": aggregate_health,
            "stacks": stacks,
            "containers": containers,
        }
    except Exception as exc:
        return {"available": False, "error": str(exc), "containers": [], "stacks": []}


def docker_networks_info():
    networks = docker_request("GET", "/networks") or []
    result = []
    for summary in networks:
        item = summary
        network_id = str(summary.get("Id", ""))
        if network_id:
            try:
                inspected = docker_request("GET", f"/networks/{quote(network_id, safe='')}")
                if isinstance(inspected, dict):
                    item = {**summary, **inspected}
            except Exception as exc:
                print(f"[docker] Network inspect failed for {network_id[:12]}: {exc}")
        configs = (item.get("IPAM") or {}).get("Config") or []
        containers = item.get("Containers")
        if not isinstance(containers, dict):
            containers = {}
        name = str(item.get("Name", ""))
        container_names = sorted(
            {
                str(endpoint.get("Name", "")).lstrip("/")
                for endpoint in containers.values()
                if isinstance(endpoint, dict) and endpoint.get("Name")
            },
            key=str.lower,
        )
        result.append({
            "id": str(item.get("Id", network_id)),
            "name": name,
            "driver": str(item.get("Driver", "unknown")),
            "scope": str(item.get("Scope", "local")),
            "subnets": [config.get("Subnet", "") for config in configs if config.get("Subnet")],
            "gateways": [config.get("Gateway", "") for config in configs if config.get("Gateway")],
            "containers": len(containers),
            "containerNames": container_names,
            "internal": bool(item.get("Internal")),
            "attachable": bool(item.get("Attachable")),
            "ingress": bool(item.get("Ingress")),
            "builtin": name in ("bridge", "host", "none") or bool(item.get("Ingress")),
            "labels": item.get("Labels") or {},
        })
    return sorted(result, key=lambda item: (not item["builtin"], item["name"].lower()))


def parse_image_reference(reference):
    if not reference or reference.startswith("sha256:") or "@" in reference:
        return None
    parts = reference.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        registry = parts.pop(0)
    else:
        registry = "registry-1.docker.io"
    image = "/".join(parts)
    last = image.rsplit("/", 1)[-1]
    if ":" in last:
        image, tag = image.rsplit(":", 1)
    else:
        tag = "latest"
    if registry == "registry-1.docker.io" and "/" not in image:
        image = f"library/{image}"
    return registry, image, tag


def remote_manifest_digest(registry, repository, tag):
    url = f"https://{registry}/v2/{quote(repository, safe='/')}/manifests/{quote(tag, safe='')}"
    headers = {
        "User-Agent": f"ubuntu-dashboard/{VERSION}",
        "Accept": (
            "application/vnd.oci.image.index.v1+json, "
            "application/vnd.docker.distribution.manifest.list.v2+json, "
            "application/vnd.oci.image.manifest.v1+json, "
            "application/vnd.docker.distribution.manifest.v2+json"
        ),
    }

    def request_digest(auth_header=None):
        request_headers = dict(headers)
        if auth_header:
            request_headers["Authorization"] = auth_header
        request = urllib.request.Request(url, headers=request_headers, method="HEAD")
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.headers.get("Docker-Content-Digest")

    try:
        return request_digest()
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise
        challenge = exc.headers.get("WWW-Authenticate", "")
        if not challenge.lower().startswith("bearer "):
            raise
        options = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        realm = options.pop("realm", "")
        if not realm:
            raise
        token_url = f"{realm}?{urlencode(options)}"
        token_request = urllib.request.Request(token_url, headers={"User-Agent": headers["User-Agent"]})
        with urllib.request.urlopen(token_request, timeout=5) as response:
            token_payload = json.loads(response.read())
        token = token_payload.get("token") or token_payload.get("access_token")
        return request_digest(f"Bearer {token}")


def docker_image_updates():
    docker = docker_info()
    if not docker.get("available"):
        return {"available": False, "containers": {}}
    image_results = {}
    container_results = {}
    references = list(dict.fromkeys(container["image"] for container in docker["containers"][:40]))

    def check_image(reference):
        parsed = parse_image_reference(reference)
        if not parsed:
            return reference, {"updateAvailable": None}
        try:
            inspect = docker_request("GET", f"/images/{quote(reference, safe='')}/json") or {}
            local_digests = {
                digest.rsplit("@", 1)[-1] for digest in inspect.get("RepoDigests", []) if "@" in digest
            }
            remote_digest = remote_manifest_digest(*parsed)
            return reference, {
                "updateAvailable": bool(local_digests and remote_digest and remote_digest not in local_digests),
                "remoteDigest": remote_digest,
            }
        except Exception as exc:
            return reference, {"updateAvailable": None, "error": str(exc)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        for reference, result in executor.map(check_image, references):
            image_results[reference] = result

    for container in docker["containers"][:40]:
        reference = container["image"]
        container_results[container["fullId"]] = image_results[reference]
    return {"available": True, "containers": container_results}


def host_mount_text():
    for source in (
        HOST_MOUNTS_FILE,
        host_path("/proc/1/mounts"),
        host_path("/proc/mounts"),
        host_path("/etc/mtab"),
    ):
        content = read_text(source)
        if content.strip():
            return content
    return ""


def mount_records():
    records = []
    for line in host_mount_text().splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        device, mount, filesystem = parts[:3]
        records.append({
            "device": device.replace("\\040", " "),
            "mount": mount.replace("\\040", " ").replace("\\011", "\t"),
            "filesystem": filesystem,
        })
    return records


def filesystem_usage(mount):
    try:
        stats = os.statvfs(host_path(mount))
        total = stats.f_blocks * stats.f_frsize
        available = stats.f_bavail * stats.f_frsize
        used = max(0, total - stats.f_bfree * stats.f_frsize)
        return {
            "total": total,
            "used": used,
            "available": available,
            "percent": round(used / total * 100, 1) if total else 0,
        }
    except OSError:
        return {"total": 0, "used": 0, "available": 0, "percent": 0}


def parse_unraid_disks():
    source = read_text(host_path("/var/local/emhttp/disks.ini"))
    if not source.strip():
        return []
    sections, current = [], None
    for raw in source.splitlines():
        line = raw.strip()
        section = re.fullmatch(r'\["?([^]"]+)"?\]', line)
        if section:
            current = {"section": section.group(1)}
            sections.append(current)
            continue
        if current and "=" in line:
            key, value = line.split("=", 1)
            current[key.strip()] = value.strip().strip('"')
    return sections


def numeric_size(value):
    try:
        number = int(float(value or 0))
        # Unraid reports drive sizes in KiB.
        return number * 1024 if number and number < 10 ** 14 else number
    except (TypeError, ValueError):
        return 0


def normalize_disk_state(raw, temperature=None, smart_passed=None, disk_type="hdd"):
    state = str(raw or "").lower()
    try:
        temperature = float(temperature) if temperature not in (None, "", "*") else None
    except (TypeError, ValueError):
        temperature = None
    if smart_passed is False or any(word in state for word in (
        "fault", "fail", "error", "offline", "disabled", "invalid",
        "disk_np", "disk_dsbl", "disk_wrong", "red",
        "faulted", "unavail", "removed",
    )):
        return "critical"
    if any(word in state for word in (
        "warn", "degrad", "rebuild", "emulated", "missing", "yellow", "orange",
        "degraded", "resilvering",
    )):
        return "warning"
    # NVMe and SSD have higher temperature tolerance
    if disk_type == "nvme":
        warning_temperature, critical_temperature = 75, 85
    elif disk_type == "ssd":
        warning_temperature, critical_temperature = 65, 75
    else:
        warning_temperature, critical_temperature = 50, 60
    if temperature is not None and temperature >= critical_temperature:
        return "critical"
    if temperature is not None and temperature >= warning_temperature:
        return "warning"
    return "healthy"


def unraid_disk_telemetry():
    result = []
    for item in parse_unraid_disks():
        name = item.get("name") or item.get("section", "")
        device = item.get("device", "").removeprefix("/dev/")
        if not name or name in ("flash", "user", "user0"):
            continue
        temperature = item.get("temp")
        try:
            temperature = round(float(temperature), 1)
        except (TypeError, ValueError):
            temperature = None
        raw_status = item.get("status") or item.get("color") or item.get("state") or "running"
        disk_type = "ssd" if item.get("rotational") == "0" or device.startswith("nvme") else "hdd"
        result.append({
            "name": name,
            "device": device,
            "model": item.get("id") or item.get("model") or device,
            "type": disk_type,
            "state": "standby" if temperature is None else "running",
            "health": normalize_disk_state(raw_status, temperature, disk_type=disk_type),
            "temperature": temperature,
            "size": numeric_size(item.get("size")),
        })
    return result


def physical_block_names():
    result = []
    base = host_path("/sys/block")
    try:
        entries = base.iterdir()
    except OSError:
        return result
    for entry in entries:
        name = entry.name
        if re.match(r"^(sd[a-z]+|hd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme\d+n\d+|mmcblk\d+)$", name):
            result.append(name)
    return sorted(result)


def smart_data(name):
    smartctl = shutil.which("smartctl")
    device = host_path(f"/dev/{name}")
    if not smartctl or not device.exists():
        return {}
    try:
        completed = subprocess.run(
            [smartctl, "--json=c", "-n", "standby", "-H", "-A", str(device)],
            capture_output=True, text=True, timeout=4,
        )
        payload = json.loads(completed.stdout or "{}")
        standby = completed.returncode == 2 or "standby" in (payload.get("smartctl", {}).get("messages") or [{}])[0].get("string", "").lower()
        temperature = payload.get("temperature", {}).get("current")
        if temperature is None:
            for attribute in payload.get("ata_smart_attributes", {}).get("table", []):
                if attribute.get("id") in (190, 194):
                    temperature = attribute.get("raw", {}).get("value")
                    break
        return {
            "temperature": temperature,
            "smartPassed": payload.get("smart_status", {}).get("passed"),
            "standby": standby,
            "model": payload.get("model_name"),
            "serial": payload.get("serial_number"),
        }
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return {}


def sysfs_disk(name):
    base = host_path(f"/sys/block/{name}")
    smart = smart_data(name)
    try:
        size = int(read_text(base / "size", "0").strip() or 0) * 512
    except ValueError:
        size = 0
    state = read_text(base / "device/state", "running").strip().lower() or "running"
    if state in ("live", "active", "online"):
        state = "running"
    elif state in ("suspended", "sleeping"):
        state = "standby"
    temperature = smart.get("temperature")
    try:
        temperature = round(float(temperature), 1)
    except (TypeError, ValueError):
        temperature = None
    rotational = read_text(base / "queue/rotational", "1").strip()
    # Detect NVMe explicitly
    is_nvme = name.startswith("nvme")
    if is_nvme:
        disk_type = "nvme"
    elif rotational == "0":
        disk_type = "ssd"
    else:
        disk_type = "hdd"
    model = smart.get("model") or read_text(base / "device/model", name).strip() or name
    return {
        "name": name,
        "device": name,
        "model": model,
        "serial": smart.get("serial") or read_text(base / "device/serial").strip(),
        "type": disk_type,
        "rotational": rotational != "0",
        "state": "standby" if smart.get("standby") else state,
        "health": normalize_disk_state(state, temperature, smart.get("smartPassed"), disk_type),
        "temperature": temperature,
        "size": size,
    }


def disk_telemetry():
    unraid = unraid_disk_telemetry()
    if unraid:
        return unraid
    names = physical_block_names()
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, len(names)))) as executor:
        return list(executor.map(sysfs_disk, names))


def host_command(command, timeout=5):
    executable = command[0]
    candidates = (f"/usr/sbin/{executable}", f"/sbin/{executable}", f"/usr/bin/{executable}", f"/bin/{executable}")
    host_executable = next((item for item in candidates if host_path(item).exists()), None)
    if not host_executable:
        return ""
    try:
        completed = subprocess.run(
            ["chroot", str(HOST_ROOT), host_executable, *command[1:]],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
        return completed.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def member_from_disk(disk, role=None, vdev=None):
    return {
        "name": disk.get("name") or disk.get("device"),
        "device": disk.get("device", ""),
        "role": role or "disk",
        "vdev": vdev,
        "status": disk.get("health", "healthy"),
        "state": disk.get("state", "running"),
        "temperature": disk.get("temperature"),
        "size": disk.get("size", 0),
        "type": disk.get("type", "hdd"),
    }


def base_disk_name(device):
    name = Path(str(device)).name
    if re.match(r"^nvme\d+n\d+p\d+$", name) or re.match(r"^mmcblk\d+p\d+$", name):
        return re.sub(r"p\d+$", "", name)
    return re.sub(r"\d+$", "", name)


def unraid_storage(disks):
    metadata = parse_unraid_disks()
    if not metadata:
        return []
    telemetry = {item["name"]: item for item in disks}
    array_members, pool_members = [], {}
    for item in metadata:
        name = item.get("name") or item.get("section", "")
        if not name or name in ("flash", "user", "user0"):
            continue
        role = "parity" if name.startswith("parity") else "data"
        pool = item.get("pool") or item.get("poolName")
        if name.startswith("cache") or pool:
            pool_name = pool or "cache"
            pool_members.setdefault(pool_name, []).append(member_from_disk(telemetry.get(name, {
                "name": name, "device": item.get("device", ""), "size": numeric_size(item.get("size")),
            }), "cache"))
        else:
            member = member_from_disk(telemetry.get(name, {
                "name": name, "device": item.get("device", ""), "size": numeric_size(item.get("size")),
            }), role)
            mount = f"/mnt/{name}"
            member.update(filesystem_usage(mount) if role == "data" else {"used": 0, "percent": 0})
            array_members.append(member)
    groups = []
    data_members = [item for item in array_members if item["role"] == "data"]
    usage = filesystem_usage("/mnt/user")
    if not usage["total"]:
        usage = {
            "total": sum(item.get("total", item.get("size", 0)) for item in data_members),
            "used": sum(item.get("used", 0) for item in data_members),
            "available": 0,
            "percent": 0,
        }
        usage["available"] = max(0, usage["total"] - usage["used"])
        usage["percent"] = round(usage["used"] / usage["total"] * 100, 1) if usage["total"] else 0
    groups.append({"name": "Array", "type": "Unraid array", "status": "healthy", "members": array_members, **usage})
    for name, members in pool_members.items():
        usage = filesystem_usage(f"/mnt/{name}")
        if not usage["total"]:
            usage["total"] = sum(item["size"] for item in members)
            usage["available"] = usage["total"]
        groups.append({"name": name, "type": "Cache / pool", "status": "healthy", "members": members, **usage})
    for group in groups:
        health = [item["status"] for item in group["members"]]
        group["status"] = "critical" if "critical" in health else "warning" if "warning" in health else "healthy"
    return groups


def zfs_storage(disks):
    listing = host_command(["zpool", "list", "-Hp", "-o", "name,size,alloc,free,cap,frag,dedup,health"])
    if not listing.strip():
        return []
    disk_map = {item["device"]: item for item in disks}
    status_output = host_command(["zpool", "status", "-P"])
    members_by_pool = {}
    current_pool = current_vdev = None
    vdev_types_by_pool = {}
    for raw in status_output.splitlines():
        if raw.startswith("  pool:"):
            current_pool = raw.split(":", 1)[1].strip()
            current_vdev = None
            members_by_pool.setdefault(current_pool, [])
            continue
        match = re.match(r"^(\s+)(\S+)\s+(ONLINE|DEGRADED|FAULTED|OFFLINE|UNAVAIL|REMOVED)\b", raw)
        if not match or not current_pool:
            continue
        indent, node, state = len(match.group(1)), match.group(2), match.group(3)
        if indent <= 4 or node == current_pool:
            continue
        if not node.startswith(("/dev/", "ata-", "wwn-", "nvme-")):
            current_vdev = node
            vdev_types_by_pool.setdefault(current_pool, set()).add(re.split(r"[-\d]", node)[0])
            continue
        device = Path(node).name
        base = base_disk_name(device)
        disk = disk_map.get(base, {"name": device, "device": device, "size": 0, "health": "healthy", "state": "running", "temperature": None})
        member = member_from_disk(disk, "data", current_vdev)
        topology_status = normalize_disk_state(state)
        member["status"] = topology_status if topology_status != "healthy" else disk.get("health", "healthy")
        members_by_pool[current_pool].append(member)
    result = []
    for line in listing.splitlines():
        parts = line.split("\t")
        if len(parts) < 8:
            parts = line.split()
        if len(parts) < 8:
            continue
        name, size, allocated, free, capacity, frag, dedup, health = parts[:8]
        try:
            total, used, available = int(size), int(allocated), int(free)
            percent = float(str(capacity).rstrip("%"))
            frag_val = float(str(frag).rstrip("%")) if frag not in ("-", "", "no") else None
            dedup_val = str(dedup).rstrip("x") if dedup not in ("-", "") else None
        except ValueError:
            continue
        vdev_types = vdev_types_by_pool.get(name, set())
        if "raidz3" in vdev_types:
            raid_type = "RAIDZ-3"
        elif "raidz2" in vdev_types:
            raid_type = "RAIDZ-2"
        elif "raidz1" in vdev_types or "raidz" in vdev_types:
            raid_type = "RAIDZ-1"
        elif "mirror" in vdev_types:
            raid_type = "Mirror"
        elif "draid" in vdev_types:
            raid_type = "dRAID"
        else:
            raid_type = "Stripe"
        result.append({
            "name": name,
            "type": "ZFS pool",
            "raidType": raid_type,
            "status": normalize_disk_state(health),
            "total": total,
            "used": used,
            "available": available,
            "percent": percent,
            "fragmentation": frag_val,
            "dedup": dedup_val,
            "members": members_by_pool.get(name, []),
        })
    return result


def zfs_pool_scrub_status(pool_name):
    """Parse zpool status for a specific pool and return scrub progress info."""
    output = host_command(["zpool", "status", "-P", pool_name])
    result = {"pool": pool_name, "scrub": {"state": "none"}, "errors": "none"}
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("scan:"):
            scan_info = stripped[len("scan:"):].strip()
            if "scrub in progress" in scan_info or "resilver in progress" in scan_info:
                progress_match = re.search(r"(\d+\.\d+)%", scan_info)
                speed_match = re.search(r"([\d.]+ [KMGT]?B/s)", scan_info)
                eta_match = re.search(r"(\d+ days? )?(\d+h\d+m|\d+:\d+:\d+) to go", scan_info)
                result["scrub"] = {
                    "state": "scanning",
                    "progress": float(progress_match.group(1)) if progress_match else 0,
                    "speed": speed_match.group(1) if speed_match else None,
                    "eta": (eta_match.group(0).replace(" to go", "") if eta_match else None),
                }
            elif "scrub repaired" in scan_info or "repaired" in scan_info:
                errors_match = re.search(r"(\d+) error", scan_info)
                date_match = re.search(r"on (.+)$", scan_info)
                result["scrub"] = {
                    "state": "finished",
                    "errors": int(errors_match.group(1)) if errors_match else 0,
                    "completedAt": date_match.group(1).strip() if date_match else None,
                }
            elif "scrub canceled" in scan_info:
                result["scrub"] = {"state": "canceled"}
        if stripped.startswith("errors:"):
            result["errors"] = stripped[len("errors:"):].strip()
    return result


def zfs_datasets(pool_name=None):
    """List ZFS datasets with usage info."""
    cmd = ["zfs", "list", "-Hp", "-o", "name,used,avail,refer,mountpoint,type,compression,compressratio"]
    if pool_name:
        cmd.append(pool_name)
    output = host_command(cmd, timeout=10)
    if not output.strip():
        return []
    datasets = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 8:
            parts = line.split()
        if len(parts) < 6:
            continue
        name, used, avail, refer, mountpoint, ds_type = parts[:6]
        compression = parts[6] if len(parts) > 6 else "-"
        compressratio = parts[7] if len(parts) > 7 else "-"
        try:
            datasets.append({
                "name": name,
                "pool": name.split("/")[0],
                "type": ds_type,
                "used": int(used),
                "available": int(avail),
                "refer": int(refer),
                "mountpoint": mountpoint if mountpoint != "-" else None,
                "compression": compression if compression != "off" else None,
                "compressRatio": compressratio.rstrip("x") if compressratio not in ("-", "1.00x") else None,
            })
        except (ValueError, IndexError):
            continue
    return datasets


def md_storage(disks):
    disk_map = {item["device"]: item for item in disks}
    records = mount_records()
    result = []
    lines = read_text(host_path("/proc/mdstat")).splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(md\d+)\s*:\s*(\w+)\s+(\S+)\s+(.+)$", line)
        if not match:
            continue
        name, array_state, raid_level, devices = match.groups()
        members = []
        # Track faulty devices (marked with F)
        faulty_devices = set()
        for token in devices.split():
            device_match = re.match(r"([A-Za-z0-9_-]+?)(?:p?\d+)?\[(\d+)\](\([A-Z]\))?$", token)
            if not device_match:
                continue
            device = device_match.group(1)
            flags = device_match.group(3) or ""
            is_faulty = "F" in flags
            is_spare = "S" in flags
            if is_faulty:
                faulty_devices.add(device)
            disk = disk_map.get(device, {"name": device, "device": device, "size": 0, "health": "healthy", "state": "running", "temperature": None})
            role = "spare" if is_spare else "parity" if raid_level in ("raid1", "raid5", "raid6", "raid10") and len(members) == 0 and raid_level != "raid1" else "data"
            member = member_from_disk(disk, role, raid_level)
            if is_faulty:
                member["status"] = "critical"
            members.append(member)
        detail_line = lines[index + 1] if index + 1 < len(lines) else ""
        degraded = "_" in detail_line or "inactive" in array_state.lower() or bool(faulty_devices)
        rebuilding = "recovery" in detail_line or "resync" in detail_line
        # Parse array status from detail line
        active_match = re.search(r"(\d+)/(\d+)", detail_line)
        active_disks = int(active_match.group(1)) if active_match else len(members)
        total_disks = int(active_match.group(2)) if active_match else len(members)
        # Determine status
        if faulty_devices or (active_match and active_disks < total_disks):
            status = "critical" if (total_disks - active_disks) > 1 else "warning"
        elif rebuilding:
            status = "warning"
        elif degraded:
            status = "warning"
        else:
            status = "healthy"
        # Determine RAID type label
        raid_pretty = {
            "raid0": "RAID-0 (Stripe)", "raid1": "RAID-1 (Mirror)",
            "raid4": "RAID-4", "raid5": "RAID-5",
            "raid6": "RAID-6", "raid10": "RAID-10",
            "linear": "Linear", "multipath": "Multipath",
        }.get(raid_level.lower(), raid_level.upper())
        mount = next((item["mount"] for item in records if item["device"] in (f"/dev/{name}", f"/dev/md/{name}")), "")
        usage = filesystem_usage(mount) if mount else {"total": sum(item["size"] for item in members), "used": 0, "available": 0, "percent": 0}
        result.append({
            "name": name,
            "type": f"Linux MD RAID",
            "raidType": raid_pretty,
            "status": status,
            "members": members,
            "activeDrives": active_disks,
            "totalDrives": total_disks,
            **usage,
        })
    return result


def lvm_storage(disks):
    disk_map = {item["device"]: item for item in disks}
    records = mount_records()
    result, seen = [], set()
    class_block = host_path("/sys/class/block")
    try:
        dm_devices = class_block.glob("dm-*")
    except OSError:
        return result
    for dm in dm_devices:
        logical_name = read_text(dm / "dm/name").strip()
        if not logical_name:
            continue
        record = next((item for item in records if Path(item["device"]).name in (dm.name, logical_name)), None)
        if not record or not (record["mount"] == "/" or record["mount"].startswith(("/mnt/", "/media/", "/srv/", "/data/", "/storage/"))):
            continue
        members = []
        try:
            slaves = list((dm / "slaves").iterdir())
        except OSError:
            slaves = []
        for slave in slaves:
            base = base_disk_name(slave.name)
            if base in disk_map:
                members.append(member_from_disk(disk_map[base], "physical volume", logical_name))
        if not members:
            continue
        key = tuple(sorted(item["device"] for item in members))
        if key in seen:
            continue
        seen.add(key)
        usage = filesystem_usage(record["mount"])
        health = [item["status"] for item in members]
        result.append({
            "name": logical_name,
            "type": "LVM volume group",
            "status": "critical" if "critical" in health else "warning" if "warning" in health else "healthy",
            "members": members,
            **usage,
        })
    return result


def btrfs_storage(disks):
    disk_map = {item["device"]: item for item in disks}
    btrfs_root = host_path("/sys/fs/btrfs")
    mounts = [item for item in mount_records() if item["filesystem"] == "btrfs"]
    result, claimed = [], set()
    try:
        filesystems = [item for item in btrfs_root.iterdir() if (item / "devices").is_dir()]
    except OSError:
        filesystems = []
    for filesystem in filesystems:
        members = []
        try:
            devices = list((filesystem / "devices").iterdir())
        except OSError:
            devices = []
        for entry in devices:
            raw_name = read_text(entry / "name").strip()
            base = base_disk_name(raw_name)
            disk = disk_map.get(base)
            if disk:
                members.append(member_from_disk(disk, "data", "Btrfs"))
                claimed.add(base)
        if not members:
            continue
        mount = next((
            item["mount"] for item in mounts
            if base_disk_name(item["device"]) in {member["device"] for member in members}
        ), "")
        usage = filesystem_usage(mount) if mount else {
            "total": sum(item["size"] for item in members),
            "used": 0,
            "available": sum(item["size"] for item in members),
            "percent": 0,
        }
        label = read_text(filesystem / "label").strip()
        health = [item["status"] for item in members]
        result.append({
            "name": label or (Path(mount).name if mount and mount != "/" else "Btrfs"),
            "type": "Btrfs filesystem",
            "status": "critical" if "critical" in health else "warning" if "warning" in health else "healthy",
            "members": members,
            **usage,
        })
    return result


def generic_storage(disks):
    records = mount_records()
    groups = []
    for disk in disks:
        candidates = []
        for record in records:
            device_name = Path(record["device"]).name
            if device_name == disk["device"] or device_name.startswith(disk["device"] + "p") or re.match(rf"^{re.escape(disk['device'])}\d+$", device_name):
                if record["mount"] == "/" or record["mount"].startswith(("/mnt/", "/media/", "/srv/", "/data/", "/storage/")):
                    candidates.append(record)
        selected = max(candidates, key=lambda item: filesystem_usage(item["mount"])["total"], default=None)
        usage = filesystem_usage(selected["mount"]) if selected else {
            "total": disk["size"], "used": 0, "available": disk["size"], "percent": 0,
        }
        disk_type = disk.get("type", "hdd").upper()
        type_label = {
            "NVME": "NVMe SSD", "SSD": "SATA SSD", "HDD": "Hard Drive"
        }.get(disk_type, disk_type)
        role = "system" if selected and selected["mount"] == "/" else "data"
        groups.append({
            "name": disk["model"] or disk["device"],
            "type": f"{type_label} · /dev/{disk['device']}",
            "status": disk["health"],
            "members": [member_from_disk(disk, role)],
            **usage,
        })
    return groups


def storage_info():
    disks = cached("disk-telemetry", 10, disk_telemetry)
    unraid = unraid_storage(disks)
    if unraid:
        return unraid
    groups = zfs_storage(disks) + md_storage(disks) + lvm_storage(disks) + btrfs_storage(disks)
    claimed = {
        member["device"]
        for group in groups
        for member in group.get("members", [])
    }
    groups.extend(generic_storage([disk for disk in disks if disk["device"] not in claimed]))
    return groups


def valid_discord_webhook(url):
    try:
        parsed = urlparse(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname in ("discord.com", "ptb.discord.com", "canary.discord.com")
            and bool(re.fullmatch(r"/api/webhooks/\d+/[A-Za-z0-9._-]+/?", parsed.path))
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        return False


def normalized_mention(value):
    mention = " ".join(str(value or "").strip().split())
    if not mention:
        return ""
    if mention.isdigit():
        return f"<@&{mention}>"
    tokens = mention.split()
    if all(token in ("@everyone", "@here") or re.fullmatch(r"<@&?\d+>|<@!\d+>", token) for token in tokens):
        return mention
    raise ValueError("Use a role/user ID, <@&ROLE_ID>, <@USER_ID>, @here or @everyone.")


def allowed_mentions(mention):
    roles = list(dict.fromkeys(re.findall(r"<@&(\d+)>", mention)))[:100]
    users = list(dict.fromkeys(re.findall(r"<@!?(\d+)>", mention)))[:100]
    result = {"parse": ["everyone"] if "@everyone" in mention or "@here" in mention else []}
    if roles:
        result["roles"] = roles
    if users:
        result["users"] = users
    return result


def safe_webhook_error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return f"Discord returned HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return f"Discord connection failed: {str(exc.reason)[:180]}"
    message = str(exc)
    webhook = _notification_settings.get("webhookUrl", "")
    return (message.replace(webhook, "[webhook]") if webhook else message)[:240]


def send_discord_notification(title, description, severity="warning", category="System"):
    with _notification_lock:
        settings = dict(_notification_settings)
    webhook = settings.get("webhookUrl", "")
    if not webhook or not valid_discord_webhook(webhook):
        raise ValueError("No valid Discord webhook is configured.")
    mention = normalized_mention(settings.get("mention", ""))
    color = 0xF25F68 if severity == "critical" else 0xE8AD43 if severity == "warning" else 0x43D17A
    payload = {
        "username": "Ubuntu Dashboard Watchdog",
        "content": mention,
        "allowed_mentions": allowed_mentions(mention),
        "embeds": [{
            "title": title[:256],
            "description": description[:3500],
            "color": color,
            "fields": [
                {"name": "Host", "value": read_text(host_path("/etc/hostname"), socket.gethostname()).strip()[:1024], "inline": True},
                {"name": "Category", "value": category[:1024], "inline": True},
                {"name": "Severity", "value": severity.upper(), "inline": True},
            ],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }],
    }
    separator = "&" if "?" in webhook else "?"
    request = urllib.request.Request(
        f"{webhook}{separator}wait=true",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": f"ubuntu-dashboard/{VERSION}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            if response.status not in (200, 204):
                raise RuntimeError(f"Discord returned HTTP {response.status}")
        with _notification_lock:
            _notification_runtime["lastSent"] = int(time.time())
            _notification_runtime["lastError"] = ""
        return True
    except Exception as exc:
        safe_error = safe_webhook_error(exc)
        with _notification_lock:
            _notification_runtime["lastError"] = safe_error
        raise RuntimeError(safe_error) from None


def collect_alert_issues(system, docker, storage):
    with _notification_lock:
        settings = dict(_notification_settings)
    issues = {}
    if settings.get("diskAlerts"):
        for disk in system.get("disks", []):
            if disk.get("health") in ("warning", "critical"):
                temperature = f" · {disk['temperature']} °C" if disk.get("temperature") is not None else ""
                issues[f"disk:{disk.get('device')}"] = {
                    "category": "Disks",
                    "severity": disk["health"],
                    "title": f"Disk problem: {disk.get('name')}",
                    "description": f"`/dev/{disk.get('device')}` · {disk.get('model')}{temperature}",
                    "delay": 0,
                }
    if settings.get("containerAlerts"):
        if docker.get("available"):
            for stack in docker.get("stacks", []):
                if stack.get("health") in ("warning", "critical"):
                    problem_lines = "\n".join(
                        f"• **{item.get('name')}** — {item.get('status')}"
                        for item in stack.get("problems", [])
                    )
                    issues[f"stack:{stack.get('name')}"] = {
                        "category": "Containers",
                        "severity": stack["health"],
                        "title": f"Docker stack problem: {stack.get('name')}",
                        "description": (
                            f"{stack.get('running', 0)}/{stack.get('total', 0)} containers are running."
                            + (f"\n{problem_lines}" if problem_lines else "")
                        ),
                        "delay": 5,
                    }
            for container in docker.get("containers", []):
                if not container.get("stack") and (
                    container.get("health") == "unhealthy" or container.get("state") in ("restarting", "dead")
                ):
                    issues[f"container:{container.get('fullId')}"] = {
                        "category": "Containers",
                        "severity": "critical" if container.get("health") == "unhealthy" or container.get("state") == "dead" else "warning",
                        "title": f"Container problem: {container.get('name')}",
                        "description": container.get("status") or container.get("state", "Unknown Docker state"),
                        "delay": 5,
                    }
    if settings.get("systemAlerts"):
        if not docker.get("available"):
            issues["system:docker"] = {
                "category": "System", "severity": "critical", "title": "Docker is unavailable",
                "description": docker.get("error", "The Docker daemon cannot be reached."), "delay": 10,
            }
        if system.get("cpu", {}).get("percent", 0) >= 95:
            issues["system:cpu"] = {
                "category": "System", "severity": "warning", "title": "Sustained high CPU load",
                "description": f"CPU usage is {system['cpu']['percent']}%.", "delay": 30,
            }
        if system.get("memory", {}).get("percent", 0) >= 95:
            issues["system:memory"] = {
                "category": "System", "severity": "warning", "title": "Memory almost exhausted",
                "description": f"Memory usage is {system['memory']['percent']}%.", "delay": 30,
            }
        for group in storage:
            percent = float(group.get("percent", 0))
            if percent >= 90:
                issues[f"system:storage:{group.get('name')}"] = {
                    "category": "System",
                    "severity": "critical" if percent >= 98 else "warning",
                    "title": f"Storage capacity warning: {group.get('name')}",
                    "description": f"{percent}% of {bytes_label(group.get('total', 0))} is in use.",
                    "delay": 0,
                }
    return issues


def evaluate_alerts(system, docker, storage):
    global _notification_state
    with _notification_lock:
        settings = dict(_notification_settings)
    if not settings.get("enabled") or not settings.get("webhookUrl"):
        with _notification_lock:
            _notification_state = {}
        return
    now = time.time()
    issues = collect_alert_issues(system, docker, storage)
    repeat = max(300, int(settings.get("repeatMinutes", 60)) * 60)
    pending = []
    with _notification_lock:
        for key, issue in issues.items():
            signature = (issue["severity"], issue["title"], issue["description"])
            previous = _notification_state.get(key)
            if not previous or previous["signature"] != signature:
                previous = {"signature": signature, "firstSeen": now, "lastSent": 0}
                _notification_state[key] = previous
            if now - previous["firstSeen"] >= issue.get("delay", 0) and now - previous["lastSent"] >= repeat:
                previous["lastSent"] = now
                pending.append((key, issue))
        for key in list(_notification_state):
            if key not in issues:
                _notification_state.pop(key, None)
    for key, issue in pending:
        try:
            send_discord_notification(issue["title"], issue["description"], issue["severity"], issue["category"])
        except Exception as exc:
            with _notification_lock:
                if key in _notification_state:
                    _notification_state[key]["lastSent"] = time.time() - repeat + 60
            print(f"[notifications] Discord webhook failed: {exc}")


def schedule_alert_evaluation(system, docker, storage):
    global _notification_pending
    with _notification_lock:
        if _notification_pending or not _notification_settings.get("enabled"):
            return
        _notification_pending = True

    def run():
        global _notification_pending
        try:
            evaluate_alerts(system, docker, storage)
        finally:
            with _notification_lock:
                _notification_pending = False

    _notification_executor.submit(run)


def notification_monitor_loop():
    while True:
        try:
            with _notification_lock:
                enabled = _notification_settings.get("enabled") and _notification_settings.get("webhookUrl")
            if enabled:
                system = system_info()
                docker = cached("docker", 1.5, docker_info)
                storage = cached("storage", 8, storage_info)
                evaluate_alerts(system, docker, storage)
        except Exception as exc:
            print(f"[notifications] Monitor error: {exc}")
        time.sleep(10)


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
    # Try journalctl first for comprehensive system logs (journald-only modern systems)
    journalctl_output = host_command(
        ["journalctl", "-n", "300", "--no-pager", "--output=short-iso",
         "--priority=0..7"],  # all priorities
        timeout=8,
    )
    if journalctl_output.strip():
        lines = journalctl_output.splitlines()
        return {"source": "journald", "lines": lines[-300:]}

    # Fall back to log files for older systems
    candidates = [
        "/var/log/syslog",
        "/var/log/messages",
        "/var/log/kern.log",
        "/var/log/system.log",
    ]
    for candidate in candidates:
        path = host_path(candidate)
        if path.is_file():
            try:
                lines = path.read_text(errors="replace").splitlines()
                return {"source": candidate, "lines": lines[-300:]}
            except OSError:
                continue
    return {"source": "Dashboard", "lines": [
        "No classic host log file found.",
        "On journald-only systems, logs are available in the CLI tab via journalctl.",
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

    smb_files = [host_path("/etc/samba/smb.conf")]
    try:
        smb_files.extend(host_path("/etc/samba").glob("**/*.conf"))
    except OSError:
        pass
    smb = "\n".join(read_text(path) for path in dict.fromkeys(smb_files))
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

    nfs_files = [host_path("/etc/exports")]
    try:
        nfs_files.extend(host_path("/etc/exports.d").glob("*.exports"))
    except OSError:
        pass
    for exports_file in nfs_files:
        for raw in read_text(exports_file).splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            exported = line.split()[0].replace("\\040", " ")
            if exported.startswith("/"):
                add(Path(exported).name or "NFS", exported, "NFS")

    configured = [item.strip() for item in os.getenv("SHARE_ROOTS", "").split(",") if item.strip()]
    for path in configured:
        add(Path(path).name or path, path, "Konfiguriert")

    mount_lines = host_mount_text().splitlines()
    for line in mount_lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        device, mount, filesystem = parts[:3]
        mount = mount.replace("\\040", " ")
        if mount != "/" and (
            filesystem in ("nfs", "nfs4", "cifs")
            or (device.startswith("/dev/") and mount.startswith(("/mnt/", "/media/", "/srv/", "/data/", "/storage/")))
        ):
            add(Path(mount).name, mount, filesystem.upper())

    if not roots:
        distro_id = os_release_info().get("ID", "linux").lower()
        distro_paths = {
            "ubuntu": ("/home", "/srv", "/mnt", "/media", "/opt", "/data", "/storage"),
            "debian": ("/home", "/srv", "/mnt", "/media", "/opt", "/data", "/storage"),
            "fedora": ("/home", "/srv", "/var/lib/samba", "/mnt", "/media", "/opt", "/data", "/storage"),
            "rocky": ("/home", "/srv", "/var/lib/samba", "/mnt", "/media", "/opt", "/data", "/storage"),
            "rhel": ("/home", "/srv", "/var/lib/samba", "/mnt", "/media", "/opt", "/data", "/storage"),
            "arch": ("/home", "/srv", "/mnt", "/media", "/opt", "/data", "/storage"),
            "manjaro": ("/home", "/srv", "/mnt", "/media", "/opt", "/data", "/storage"),
            "opensuse": ("/home", "/srv", "/mnt", "/media", "/opt", "/data", "/storage"),
        }
        for base_path in distro_paths.get(distro_id, ("/home", "/srv", "/mnt", "/media", "/opt", "/data", "/storage")):
            add(Path(base_path).name or "root", base_path, f"{distro_id.title()}-Pfad")
    return roots


def cli_command(command):
    normalized = " ".join(command.strip().lower().split())
    if normalized in ("help", "?"):
        return (
            "Available commands:\n"
            "  system       Distribution, kernel and CPU\n"
            "  uptime       Host uptime and load\n"
            "  memory       RAM and swap usage\n"
            "  disks        Mounted storage overview\n"
            "  network      Interfaces and live traffic\n"
            "  docker ps    Docker container status\n"
            "  shares       Detected SMB/NFS/data shares\n"
            "  version      Dashboard version\n"
            "  clear        Clear this terminal"
        )
    system = system_info()
    if normalized in ("system", "uname", "uname -a"):
        return (
            f"{system['os']}\n"
            f"Kernel: {system['kernel']} ({system['architecture']})\n"
            f"CPU: {system['cpu']['model']} · {system['cpu']['cores']} cores"
        )
    if normalized == "uptime":
        return f"Uptime: {system['uptime']} seconds\nLoad: {' '.join(system['load'])}"
    if normalized in ("memory", "free", "free -h"):
        memory = system["memory"]
        return (
            f"RAM:  {bytes_label(memory['used'])} / {bytes_label(memory['total'])} ({memory['percent']}%)\n"
            f"Swap: {bytes_label(memory['swapUsed'])} / {bytes_label(memory['swapTotal'])}"
        )
    if normalized in ("disks", "df", "df -h"):
        rows = ["POOL / ARRAY           USED / TOTAL       USE%  TYPE"]
        for group in storage_info():
            rows.append(
                f"{group['name'][:20]:<20}  {bytes_label(group['used']):>8} / {bytes_label(group['total']):<8} "
                f"{group['percent']:>5}%  {group['type']}"
            )
            for member in group.get("members", []):
                rows.append(f"  {member['name'][:18]:<18} {bytes_label(member.get('size', 0)):>18}  {member.get('role', 'disk')}")
        return "\n".join(rows) if len(rows) > 1 else "No storage devices detected."
    if normalized in ("network", "ip", "ip a"):
        network = system["network"]
        return (
            f"Interfaces: {', '.join(network['interfaces']) or 'none'}\n"
            f"Download: {bytes_label(network['down'])}/s\nUpload:   {bytes_label(network['up'])}/s"
        )
    if normalized in ("docker", "docker ps"):
        docker = docker_info()
        if not docker.get("available"):
            return f"Docker unavailable: {docker.get('error')}"
        rows = ["NAME                         STATE       IMAGE"]
        for item in docker["containers"]:
            rows.append(f"{item['name'][:28]:<28} {item['state']:<11} {item['image']}")
        return "\n".join(rows)
    if normalized == "shares":
        roots = share_roots()
        return "\n".join(f"{root['protocol']:<12} {root['name']:<24} {root['path']}" for root in roots) or "No shares detected."
    if normalized in ("version", "--version"):
        return f"Ubuntu Control Dashboard {VERSION} · latest"
    raise ValueError("Command not allowed. Type 'help' for available commands.")


def host_identity_maps():
    users, groups = {}, {}
    for line in read_text(host_path("/etc/passwd")).splitlines():
        parts = line.split(":")
        if len(parts) > 2 and parts[2].isdigit():
            users[int(parts[2])] = parts[0]
    for line in read_text(host_path("/etc/group")).splitlines():
        parts = line.split(":")
        if len(parts) > 2 and parts[2].isdigit():
            groups[int(parts[2])] = parts[0]
    return users, groups


def shares_info(share_index=None, relative="", search=""):
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
    users, groups = host_identity_maps()
    search = unquote(str(search or "")).strip()[:200]
    normalized_search = search.casefold()
    try:
        items = [item for item in target.iterdir() if not normalized_search or normalized_search in item.name.casefold()]
        items.sort(key=lambda p: (not p.is_dir(), p.name.lower()))
        for item in items[:500]:
            try:
                stat_info = item.stat()
                entries.append({
                    "name": item.name,
                    "type": "directory" if item.is_dir() else "file",
                    "size": stat_info.st_size,
                    "modified": int(stat_info.st_mtime),
                    "hidden": item.name.startswith("."),
                    "owner": users.get(stat_info.st_uid, str(stat_info.st_uid)),
                    "group": groups.get(stat_info.st_gid, str(stat_info.st_gid)),
                    "permissions": stat.filemode(stat_info.st_mode),
                    "mode": format(stat_info.st_mode & 0o7777, "04o"),
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
        "search": search,
        "totalEntries": len(items),
        "truncated": len(items) > 500,
    }


def browser_target(share_index, relative="", require_exists=True):
    roots = share_roots()
    try:
        root = roots[int(share_index)]["actual"].resolve()
    except (ValueError, IndexError, TypeError):
        raise ValueError("Unknown data root")
    relative = unquote(str(relative or "")).strip("/")
    if "\x00" in relative:
        raise ValueError("Invalid path")
    candidate = root / relative
    if candidate.is_symlink():
        raise ValueError("Symbolic links cannot be modified")
    check = candidate.resolve() if candidate.exists() else candidate.parent.resolve() / candidate.name
    if check != root and root not in check.parents:
        raise ValueError("Path outside the selected data root")
    if require_exists and not candidate.exists():
        raise ValueError("File or folder not found")
    return root, candidate


def safe_file_name(value):
    name = str(value or "").strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name or len(name) > 255:
        raise ValueError("Invalid file name")
    if any(ord(character) < 32 for character in name):
        raise ValueError("Invalid file name")
    return name


def text_file_info(share_index, relative):
    _, target = browser_target(share_index, relative)
    if not target.is_file():
        raise ValueError("Not a text file")
    if target.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Files larger than 2 MB cannot be edited here")
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ValueError("This is not a UTF-8 text file")
    return {"path": str(relative).strip("/"), "name": target.name, "content": content}


class Handler(BaseHTTPRequestHandler):
    server_version = f"UbuntuDashboard/{VERSION}"

    def log_message(self, fmt, *args):
        print(f"[web] {self.address_string()} {fmt % args}")

    def current_session(self):
        if not account_configured():
            return {"username": "local", "csrf": "", "auth": "open"}
        token = cookie_value(self.headers.get("Cookie"), "dashboard_session")
        session = _sessions.get(token)
        if session and session["expires"] > time.time():
            session["expires"] = time.time() + SESSION_TTL
            session["auth"] = "session"
            return session
        return None

    def authenticated(self):
        return self.current_session() is not None

    def csrf_valid(self, session):
        return (
            not account_configured()
            or session.get("auth") == "open"
            or hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), session.get("csrf", "invalid"))
        )

    def security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' https://cdn.simpleicons.org data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self' ws: wss:; "
            "frame-src 'self' http: https:"
        )

    def send_json(self, payload, status=200, headers=None):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.security_headers()
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/ws/ssh":
            self.handle_ssh_websocket()
            return
        if path == "/api/health":
            self.send_json({"status": "ok", "version": VERSION})
            return
        if path == "/api/session":
            session = self.current_session()
            self.send_json({
                "authenticated": bool(session),
                "username": session.get("username") if session else None,
                "csrf": session.get("csrf", "") if session else "",
                "authRequired": account_configured(),
                "sshHost": ssh_target(self),
                "sshPort": SSH_PORT,
            }, 200 if session else 401)
            return
        public_files = ("/login.html", "/login.css", "/login.js")
        session = self.current_session()
        if not session:
            if path in public_files:
                self.serve_static(path)
            elif path.startswith("/api/"):
                self.send_json({"error": "Authentication required"}, 401)
            else:
                self.send_response(302)
                self.send_header("Location", "/login.html")
                self.security_headers()
                self.end_headers()
            return
        if path == "/login.html":
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if path == "/api/overview":
            system = system_info()
            docker = cached("docker", 1.5, docker_info)
            storage = cached("storage", 8, storage_info)
            schedule_alert_evaluation(system, docker, storage)
            self.send_json({
                "version": VERSION,
                "dashboardUptime": int(time.time() - STARTED),
                "system": system,
                "docker": docker,
                "storage": storage,
            })
        elif path == "/api/version":
            self.send_json(cached("github-version", 900, github_version_info))
        elif path == "/api/docker-updates":
            self.send_json(cached("docker-updates", 900, docker_image_updates))
        elif path == "/api/networks":
            try:
                self.send_json({"available": True, "networks": docker_networks_info()})
            except Exception as exc:
                self.send_json({"available": False, "networks": [], "error": str(exc)}, 503)
        elif path == "/api/account":
            self.send_json({
                "username": session.get("username", "local"),
                "persistent": account_configured() and CONFIG_FILE.is_file(),
            })
        elif path == "/api/notifications":
            with _notification_lock:
                settings = dict(_notification_settings)
                runtime = dict(_notification_runtime)
            self.send_json({
                "enabled": bool(settings["enabled"]),
                "webhookConfigured": bool(settings["webhookUrl"]),
                "mention": settings["mention"],
                "diskAlerts": bool(settings["diskAlerts"]),
                "containerAlerts": bool(settings["containerAlerts"]),
                "systemAlerts": bool(settings["systemAlerts"]),
                "repeatMinutes": settings["repeatMinutes"],
                **runtime,
            })
        elif path == "/api/iframe":
            try:
                self.send_json(public_iframe_settings())
            except (ValueError, TypeError) as exc:
                self.send_json({"error": str(exc)}, 400)
        elif path == "/api/processes":
            self.send_json({"processes": process_info()})
        elif path == "/api/logs":
            self.send_json(log_info())
        elif path in ("/api/shares", "/api/files"):
            query = parse_qs(urlparse(self.path).query)
            share = query.get("share", [None])[0]
            relative = query.get("path", [""])[0]
            search = query.get("search", [""])[0]
            try:
                self.send_json(shares_info(share, relative, search))
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
        elif path == "/api/file":
            query = parse_qs(urlparse(self.path).query)
            try:
                self.send_json(text_file_info(
                    query.get("share", [None])[0],
                    query.get("path", [""])[0],
                ))
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
        elif path.startswith("/api/zfs/scrub/"):
            pool = path.removeprefix("/api/zfs/scrub/").strip()
            if not pool or not re.match(r"^[\w\-\.]+$", pool):
                self.send_json({"error": "Invalid pool name"}, 400)
                return
            self.send_json(zfs_pool_scrub_status(pool))
        elif path == "/api/zfs/datasets":
            query = parse_qs(urlparse(self.path).query)
            pool = query.get("pool", [None])[0]
            if pool and not re.match(r"^[\w\-\.]+$", pool):
                self.send_json({"error": "Invalid pool name"}, 400)
                return
            self.send_json({"datasets": zfs_datasets(pool)})
        elif path.startswith("/api/"):
            self.send_json({"error": "Nicht gefunden"}, 404)
        else:
            self.serve_static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/login":
            self.handle_login()
            return
        session = self.current_session()
        if not session:
            self.send_json({"error": "Authentication required"}, 401)
            return
        if not self.csrf_valid(session):
            self.send_json({"error": "Invalid CSRF token"}, 403)
            return
        if path == "/api/logout":
            token = cookie_value(self.headers.get("Cookie"), "dashboard_session")
            _sessions.pop(token, None)
            cookie = "dashboard_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
            if COOKIE_SECURE:
                cookie += "; Secure"
            self.send_json({"ok": True}, headers={"Set-Cookie": cookie})
            return
        if path == "/api/account":
            self.handle_account_update(session)
            return
        if path == "/api/notifications":
            self.handle_notification_update()
            return
        if path == "/api/notifications/test":
            self.handle_notification_test()
            return
        if path == "/api/iframe":
            self.handle_iframe_update()
            return
        if path == "/api/iframe/enabled":
            self.handle_iframe_enabled()
            return
        if path == "/api/networks/create":
            self.handle_network_create()
            return
        if path == "/api/networks/delete":
            self.handle_network_delete()
            return
        if path.startswith("/api/files/"):
            self.handle_file_action(path.removeprefix("/api/files/"))
            return
        if path == "/api/cli":
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 4096)
                payload = json.loads(self.rfile.read(length) or b"{}")
                command = str(payload.get("command", ""))[:200]
                self.send_json({"output": cli_command(command)})
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json({"error": str(exc)}, 400)
            return
        if path == "/api/zfs/scrub":
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 1024)
                payload = json.loads(self.rfile.read(length) or b"{}")
                pool = str(payload.get("pool", "")).strip()
                if not pool or not re.match(r"^[\w\-\.]+$", pool):
                    self.send_json({"error": "Invalid pool name"}, 400)
                    return
                result = host_command(["zpool", "scrub", pool], timeout=10)
                _cache.pop("storage", None)
                self.send_json({"ok": True, "pool": pool, "output": result.strip()})
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
            return
        if path == "/api/zfs/scrub/stop":
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 1024)
                payload = json.loads(self.rfile.read(length) or b"{}")
                pool = str(payload.get("pool", "")).strip()
                if not pool or not re.match(r"^[\w\-\.]+$", pool):
                    self.send_json({"error": "Invalid pool name"}, 400)
                    return
                result = host_command(["zpool", "scrub", "-s", pool], timeout=10)
                _cache.pop("storage", None)
                self.send_json({"ok": True, "pool": pool, "output": result.strip()})
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
            return
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

    def handle_login(self):
        address = self.client_address[0]
        now = time.time()
        attempts = [stamp for stamp in _login_attempts.get(address, []) if now - stamp < 600]
        if len(attempts) >= 5:
            self.send_json({"error": "Too many login attempts. Try again later."}, 429)
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 4096)
            payload = json.loads(self.rfile.read(length) or b"{}")
            username = str(payload.get("username", ""))
            password = str(payload.get("password", ""))
        except (ValueError, json.JSONDecodeError):
            self.send_json({"error": "Invalid request"}, 400)
            return
        valid = verify_account(username, password)
        if not account_configured():
            valid, username = True, "local"
        if not valid:
            attempts.append(now)
            _login_attempts[address] = attempts
            time.sleep(min(1.5, 0.2 * len(attempts)))
            self.send_json({"error": "Invalid username or password"}, 401)
            return
        _login_attempts.pop(address, None)
        token, session = new_session(username)
        cookie = (
            f"dashboard_session={token}; Path=/; HttpOnly; SameSite=Strict; "
            f"Max-Age={SESSION_TTL}"
        )
        if COOKIE_SECURE:
            cookie += "; Secure"
        self.send_json({
            "ok": True,
            "username": username,
            "csrf": session["csrf"],
        }, headers={"Set-Cookie": cookie})

    def handle_file_action(self, action):
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 2 * 1024 * 1024 + 8192)
            payload = json.loads(self.rfile.read(length) or b"{}")
            share = payload.get("share")
            relative = str(payload.get("path", "")).strip("/")
            if action in ("mkdir", "create"):
                _, parent = browser_target(share, relative)
                if not parent.is_dir():
                    raise ValueError("Parent folder not found")
                name = safe_file_name(payload.get("name"))
                target = parent / name
                browser_target(share, f"{relative}/{name}".strip("/"), require_exists=False)
                if target.exists():
                    raise ValueError("A file or folder with this name already exists")
                if action == "mkdir":
                    target.mkdir(mode=0o755)
                else:
                    target.write_text(str(payload.get("content", "")), encoding="utf-8")
                parent_stat = parent.stat()
                try:
                    os.chown(target, parent_stat.st_uid, parent_stat.st_gid)
                except PermissionError:
                    pass
            elif action == "save":
                _, target = browser_target(share, relative)
                if not target.is_file() or target.stat().st_size > 2 * 1024 * 1024:
                    raise ValueError("This file cannot be edited")
                content = str(payload.get("content", ""))
                if len(content.encode()) > 2 * 1024 * 1024:
                    raise ValueError("Content exceeds the 2 MB editor limit")
                target.write_text(content, encoding="utf-8")
            elif action == "delete":
                root, target = browser_target(share, relative)
                if target == root:
                    raise ValueError("A data root cannot be deleted")
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            else:
                raise ValueError("Unknown file action")
            self.send_json({"ok": True, "action": action})
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, 400)

    def handle_account_update(self, session):
        global _account
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 8192)
            payload = json.loads(self.rfile.read(length) or b"{}")
            username = str(payload.get("username", "")).strip()
            current_password = str(payload.get("currentPassword", ""))
            new_password = str(payload.get("newPassword", ""))
        except (ValueError, json.JSONDecodeError):
            self.send_json({"error": "Invalid request"}, 400)
            return
        if not re.fullmatch(r"[A-Za-z0-9_.@-]{2,64}", username):
            self.send_json({"error": "Username must contain 2–64 valid characters."}, 400)
            return
        if account_configured() and not verify_account(_account["username"], current_password):
            self.send_json({"error": "Current password is incorrect."}, 403)
            return
        if len(new_password) < 12:
            self.send_json({"error": "The new password must contain at least 12 characters."}, 400)
            return
        updated = password_record(username, new_password)
        try:
            save_account(updated)
        except OSError as exc:
            self.send_json({"error": f"Account could not be saved: {exc}"}, 500)
            return
        _account = updated
        token = cookie_value(self.headers.get("Cookie"), "dashboard_session")
        _sessions.clear()
        session.update({
            "username": username,
            "expires": time.time() + SESSION_TTL,
            "csrf": secrets.token_urlsafe(24),
            "auth": "session",
        })
        _sessions[token] = session
        self.send_json({"ok": True, "username": username, "csrf": session["csrf"]})

    def handle_notification_update(self):
        global _notification_settings, _notification_state
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 16384)
            payload = json.loads(self.rfile.read(length) or b"{}")
            with _notification_lock:
                updated = dict(_notification_settings)
            webhook = str(payload.get("webhookUrl", "")).strip()
            if payload.get("clearWebhook"):
                updated["webhookUrl"] = ""
            elif webhook:
                if not valid_discord_webhook(webhook):
                    raise ValueError("Only a valid https://discord.com/api/webhooks/... URL is allowed.")
                updated["webhookUrl"] = webhook
            updated["mention"] = normalized_mention(payload.get("mention", ""))
            for key in ("enabled", "diskAlerts", "containerAlerts", "systemAlerts"):
                if key in payload:
                    updated[key] = bool(payload[key])
            repeat = int(payload.get("repeatMinutes", updated["repeatMinutes"]))
            updated["repeatMinutes"] = min(1440, max(5, repeat))
            if updated["enabled"] and not updated["webhookUrl"]:
                raise ValueError("Configure a Discord webhook before enabling notifications.")
            save_notification_settings(updated)
        except (ValueError, TypeError, OSError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, 400)
            return
        with _notification_lock:
            _notification_settings = updated
            _notification_state = {}
        self.send_json({
            "ok": True,
            "webhookConfigured": bool(updated["webhookUrl"]),
            "enabled": updated["enabled"],
        })

    def handle_notification_test(self):
        try:
            send_discord_notification(
                "Test alert: the watchdog is awake",
                "Discord notifications from Ubuntu Dashboard are configured correctly.",
                "healthy",
                "Test",
            )
            self.send_json({"ok": True})
        except Exception as exc:
            self.send_json({"error": f"Discord test failed: {exc}"}, 502)

    def handle_iframe_update(self):
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 65536)
            payload = json.loads(self.rfile.read(length) or b"{}")
            current = load_iframe_settings()
            targets = current["targets"]
            if "targets" in payload:
                incoming = payload["targets"]
                if not isinstance(incoming, list) or len(incoming) > 24:
                    raise ValueError("Configure at most 24 iFrame targets.")
                targets = []
                used_ids = set()
                for raw in incoming:
                    if not isinstance(raw, dict):
                        raise ValueError("Invalid iFrame target.")
                    target_id = str(raw.get("id", "")).strip()
                    if not re.fullmatch(r"[A-Za-z0-9_-]{3,64}", target_id) or target_id in used_ids:
                        target_id = secrets.token_urlsafe(8)
                    used_ids.add(target_id)
                    name = str(raw.get("name", "")).strip()[:48]
                    if not name:
                        raise ValueError("Every iFrame target needs a name.")
                    port = raw.get("port", "")
                    if port not in ("", None):
                        port = int(port)
                    target = {
                        "id": target_id,
                        "name": name,
                        "url": str(raw.get("url", "")).strip(),
                        "port": port,
                    }
                    if not iframe_source(target):
                        raise ValueError(f"URL is required for {name}.")
                    targets.append(target)
            selected_id = str(payload.get("selectedId", current["selectedId"])).strip()
            valid_ids = {target["id"] for target in targets}
            if selected_id not in valid_ids:
                selected_id = targets[0]["id"] if targets else ""
            updated = {
                "enabled": bool(payload.get("enabled", current["enabled"])),
                "targets": targets,
                "selectedId": selected_id,
            }
            save_iframe_settings(updated)
            self.send_json({"ok": True, **public_iframe_settings()})
        except (ValueError, TypeError, OSError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, 400)

    def handle_iframe_enabled(self):
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 4096)
            payload = json.loads(self.rfile.read(length) or b"{}")
            updated = load_iframe_settings()
            updated["enabled"] = bool(payload.get("enabled"))
            save_iframe_settings(updated)
            self.send_json({"ok": True, **public_iframe_settings()})
        except (OSError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, 400)

    def handle_network_create(self):
        if not ALLOW_ACTIONS:
            self.send_json({"error": "Docker actions are disabled."}, 403)
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 8192)
            payload = json.loads(self.rfile.read(length) or b"{}")
            name = str(payload.get("name", "")).strip()
            subnet_value = str(payload.get("subnet", "")).strip()
            gateway_value = str(payload.get("gateway", "")).strip()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", name):
                raise ValueError("Network name must contain 1–63 letters, numbers, dots, dashes or underscores.")
            if name in ("bridge", "host", "none"):
                raise ValueError("This name is reserved by Docker.")
            request = {
                "Name": name,
                "Driver": "bridge",
                "CheckDuplicate": True,
                "Internal": bool(payload.get("internal")),
                "Attachable": bool(payload.get("attachable", True)),
                "Labels": {"io.ubuntu-dashboard.managed": "true"},
            }
            if subnet_value:
                subnet = ipaddress.ip_network(subnet_value, strict=False)
                config = {"Subnet": str(subnet)}
                if gateway_value:
                    gateway = ipaddress.ip_address(gateway_value)
                    if gateway not in subnet:
                        raise ValueError("Gateway must be inside the selected subnet.")
                    config["Gateway"] = str(gateway)
                request["IPAM"] = {"Driver": "default", "Config": [config]}
            elif gateway_value:
                raise ValueError("Enter a subnet when specifying a gateway.")
            created = docker_request("POST", "/networks/create", request) or {}
            self.send_json({"ok": True, "id": created.get("Id", ""), "name": name})
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, 409)

    def handle_network_delete(self):
        if not ALLOW_ACTIONS:
            self.send_json({"error": "Docker actions are disabled."}, 403)
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 4096)
            payload = json.loads(self.rfile.read(length) or b"{}")
            network_id = str(payload.get("id", "")).strip()
            if not re.fullmatch(r"[A-Fa-f0-9]{12,64}", network_id):
                raise ValueError("Invalid Docker network ID.")
            network = next((item for item in docker_networks_info() if item["id"].startswith(network_id)), None)
            if not network:
                raise ValueError("Docker network was not found.")
            if network["builtin"]:
                raise ValueError("Docker's built-in networks cannot be deleted.")
            if network["containers"]:
                raise ValueError(f"Disconnect the {network['containers']} attached container(s) before deleting this network.")
            docker_request("DELETE", f"/networks/{quote(network['id'], safe='')}")
            self.send_json({"ok": True, "id": network["id"], "name": network["name"]})
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, 409)

    def handle_ssh_websocket(self):
        session = self.current_session()
        if not session:
            self.send_error(401, "Authentication required")
            return
        if self.headers.get("Upgrade", "").lower() != "websocket":
            self.send_error(400, "WebSocket required")
            return
        origin = self.headers.get("Origin", "")
        host = self.headers.get("Host", "")
        if origin and urlparse(origin).netloc != host:
            self.send_error(403, "Origin rejected")
            return
        key = self.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
        ).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True
        process = None
        master_fd = None
        try:
            opcode, raw = websocket_read(self.connection)
            if opcode != 1:
                websocket_send(self.connection, json.dumps({"type": "error", "message": "SSH login required"}))
                return
            request = json.loads(raw.decode())
            if request.get("type") != "auth":
                raise ValueError("SSH login required")
            username = str(request.get("username", ""))
            password = str(request.get("password", ""))
            if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", username) or not password or len(password) > 1024:
                raise ValueError("Invalid SSH credentials")
            cols = max(40, min(300, int(request.get("cols", 100))))
            rows = max(12, min(100, int(request.get("rows", 30))))
            master_fd, slave_fd = pty.openpty()
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            password_read, password_write = os.pipe()
            os.write(password_write, password.encode() + b"\n")
            os.close(password_write)
            environment = os.environ.copy()
            environment["TERM"] = "xterm-256color"
            target_host = ssh_target(self)
            process = subprocess.Popen([
                "sshpass", "-d", str(password_read),
                "ssh", "-tt",
                "-p", str(SSH_PORT),
                "-o", "PreferredAuthentications=password,keyboard-interactive",
                "-o", "PubkeyAuthentication=no",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "UserKnownHostsFile=/tmp/ssh_known_hosts",
                "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=30",
                "-o", "ServerAliveCountMax=3",
                f"{username}@{target_host}",
            ], stdin=slave_fd, stdout=slave_fd, stderr=slave_fd, close_fds=True,
               pass_fds=(password_read,), env=environment, start_new_session=True)
            os.close(password_read)
            os.close(slave_fd)
            websocket_send(self.connection, json.dumps({
                "type": "connected", "host": target_host, "port": SSH_PORT, "username": username
            }))
            started = time.time()
            while process.poll() is None and time.time() - started < 14400:
                readable, _, _ = select.select([master_fd, self.connection], [], [], 1)
                if master_fd in readable:
                    try:
                        output = os.read(master_fd, 65536)
                    except OSError:
                        break
                    if output:
                        websocket_send(self.connection, output, opcode=2)
                if self.connection in readable:
                    opcode, raw = websocket_read(self.connection)
                    if opcode in (None, 8):
                        break
                    if opcode == 9:
                        websocket_send(self.connection, raw, opcode=10)
                        continue
                    if opcode != 1:
                        continue
                    message = json.loads(raw.decode())
                    if message.get("type") == "input":
                        os.write(master_fd, str(message.get("data", "")).encode())
                    elif message.get("type") == "resize":
                        cols = max(40, min(300, int(message.get("cols", cols))))
                        rows = max(12, min(100, int(message.get("rows", rows))))
                        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            if process.poll() is not None:
                websocket_send(self.connection, json.dumps({"type": "exit", "code": process.returncode}))
        except Exception as exc:
            try:
                websocket_send(self.connection, json.dumps({"type": "error", "message": str(exc)}))
            except OSError:
                pass
        finally:
            if process and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=2)
                except Exception:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except OSError:
                        pass
            if master_fd is not None:
                try:
                    os.close(master_fd)
                except OSError:
                    pass

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
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.security_headers()
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    threading.Thread(target=notification_monitor_loop, name="notification-monitor", daemon=True).start()
    print(f"Ubuntu Dashboard {VERSION} läuft auf 0.0.0.0:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
