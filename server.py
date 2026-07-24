#!/usr/bin/env python3
import base64
import concurrent.futures
import fcntl
import hashlib
import hmac
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
VERSION = "1.7.0"
APP_USER = os.getenv("DASHBOARD_USER", "")
APP_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
ACCOUNT_FILE = Path(os.getenv("ACCOUNT_FILE", "/data/account.json"))
ALLOW_ACTIONS = os.getenv("ALLOW_DOCKER_ACTIONS", "true").lower() == "true"
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
SESSION_TTL = int(os.getenv("SESSION_TTL", "43200"))
SSH_HOST = os.getenv("SSH_HOST", "auto")
SSH_PORT = int(os.getenv("SSH_PORT", "22"))
STARTED = time.time()
_sample = {"at": 0, "cpu": None, "net": None}
_cache = {}
_sessions = {}
_login_attempts = {}
_account = None


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


def save_account(account):
    ACCOUNT_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = ACCOUNT_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(account))
    os.chmod(temporary, 0o600)
    temporary.replace(ACCOUNT_FILE)


def load_account():
    try:
        stored = json.loads(ACCOUNT_FILE.read_text())
        if stored.get("username") and stored.get("salt") and stored.get("passwordHash"):
            return stored
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


def host_package_updates():
    distro = os_release_info().get("ID", "").lower()
    update_commands = {
        "ubuntu": "sudo apt update && sudo apt upgrade -y",
        "debian": "sudo apt update && sudo apt upgrade -y",
        "fedora": "sudo dnf upgrade --refresh -y",
        "rocky": "sudo dnf upgrade --refresh -y",
        "rhel": "sudo dnf upgrade --refresh -y",
        "arch": "sudo pacman -Syu --noconfirm",
        "manjaro": "sudo pacman -Syu --noconfirm",
        "opensuse": "sudo zypper refresh && sudo zypper update -y",
    }
    update_command = update_commands.get(distro)
    dnf_binary = "/usr/bin/dnf" if host_path("/usr/bin/dnf").exists() else "/usr/bin/dnf5"
    commands = {
        "ubuntu": ["/usr/bin/apt", "list", "--upgradable"],
        "debian": ["/usr/bin/apt", "list", "--upgradable"],
        "fedora": [dnf_binary, "-q", "check-update", "--cacheonly"],
        "rocky": [dnf_binary, "-q", "check-update", "--cacheonly"],
        "rhel": [dnf_binary, "-q", "check-update", "--cacheonly"],
        "arch": ["/usr/bin/pacman", "-Qu"],
        "manjaro": ["/usr/bin/pacman", "-Qu"],
        "opensuse": ["/usr/bin/zypper", "--non-interactive", "list-updates"],
    }
    command = commands.get(distro)
    if not command or not host_path(command[0]).exists():
        return {"available": None, "count": None, "distro": distro, "command": update_command}
    try:
        result = subprocess.run(
            ["chroot", str(HOST_ROOT), *command],
            capture_output=True, text=True, timeout=25,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if distro in ("ubuntu", "debian"):
            updates = [line for line in lines if "/" in line and not line.lower().startswith("listing")]
        elif distro in ("fedora", "rocky", "rhel"):
            updates = [line for line in lines if len(line.split()) >= 3 and not line.startswith(("Last metadata", "Obsoleting"))]
        elif distro in ("arch", "manjaro"):
            updates = lines
        else:
            updates = [line for line in lines if "|" in line and not line.startswith(("-", "Repository", "S |"))]
        return {"available": bool(updates), "count": len(updates), "distro": distro, "command": update_command}
    except Exception as exc:
        return {"available": None, "count": None, "distro": distro, "command": update_command, "error": str(exc)}


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


def os_release_info():
    values = {}
    for line in read_text(host_path("/etc/os-release")).splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')
    return values


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
            status = item.get("Status", "")
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
                "ports": [
                    f"{p.get('PublicPort', p.get('PrivatePort'))}:{p.get('PrivatePort')}/{p.get('Type', 'tcp')}"
                    for p in item.get("Ports", []) if p.get("PrivatePort")
                ],
            })
        order = {"running": 0, "restarting": 1, "paused": 2, "exited": 3, "dead": 4}
        containers.sort(key=lambda c: (order.get(c["state"], 9), c["name"].lower()))
        aggregate_health = "unhealthy" if any(
            container["health"] == "unhealthy" for container in containers
        ) else "starting" if any(
            container["health"] == "starting" for container in containers
        ) else "healthy"
        return {
            "available": True,
            "version": info.get("ServerVersion", ""),
            "containersRunning": info.get("ContainersRunning", 0),
            "containersStopped": info.get("ContainersStopped", 0),
            "images": info.get("Images", 0),
            "health": aggregate_health,
            "containers": containers,
        }
    except Exception as exc:
        return {"available": False, "error": str(exc), "containers": []}


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

    mount_lines = read_text(host_path("/proc/mounts")).splitlines()
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
        rows = ["MOUNT                 USED / TOTAL       USE%  FILESYSTEM"]
        for disk in storage_info():
            rows.append(
                f"{disk['mount'][:20]:<20}  {bytes_label(disk['used']):>8} / {bytes_label(disk['total']):<8} "
                f"{disk['percent']:>5}%  {disk['filesystem']}"
            )
        return "\n".join(rows) if len(rows) > 1 else "No data filesystems detected."
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
    users, groups = host_identity_maps()
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
        "truncated": len(entries) == 500,
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
            "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self' ws: wss:"
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
            self.send_json({
                "version": VERSION,
                "dashboardUptime": int(time.time() - STARTED),
                "system": system_info(),
                "docker": cached("docker", 1.5, docker_info),
                "storage": cached("storage", 8, storage_info),
            })
        elif path == "/api/version":
            self.send_json(cached("github-version", 900, github_version_info))
        elif path == "/api/host-updates":
            self.send_json(cached("host-updates", 1800, host_package_updates))
        elif path == "/api/docker-updates":
            self.send_json(cached("docker-updates", 900, docker_image_updates))
        elif path == "/api/account":
            self.send_json({
                "username": session.get("username", "local"),
                "persistent": account_configured() and ACCOUNT_FILE.is_file(),
            })
        elif path == "/api/processes":
            self.send_json({"processes": process_info()})
        elif path == "/api/logs":
            self.send_json(log_info())
        elif path in ("/api/shares", "/api/files"):
            query = parse_qs(urlparse(self.path).query)
            share = query.get("share", [None])[0]
            relative = query.get("path", [""])[0]
            try:
                self.send_json(shares_info(share, relative))
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
    print(f"Ubuntu Dashboard {VERSION} läuft auf 0.0.0.0:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
