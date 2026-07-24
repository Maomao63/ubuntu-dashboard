# Ubuntu Dashboard

![Version](https://img.shields.io/badge/version-1.11.0-f05a28)
![Image](https://img.shields.io/badge/image-ghcr.io%2Fmaomao63%2Fubuntu--dashboard-blue)
![Platforms](https://img.shields.io/badge/platform-linux%2Famd64%20%7C%20linux%2Farm64-2ea44f)
[![Publish Docker image](https://github.com/Maomao63/ubuntu-dashboard/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/Maomao63/ubuntu-dashboard/actions/workflows/docker-publish.yml)

A responsive, Unraid-inspired control center for Linux servers. Ubuntu Dashboard
runs as a single Docker container and provides live host monitoring, Docker
management, storage health, a writable data browser, an SSH terminal and
configurable Discord alerts.

Despite its name, it is not limited to Ubuntu. The dashboard detects the host
distribution and adapts its branding for Ubuntu, Debian, Fedora/RHEL-based
systems, Arch-based systems, openSUSE and other Linux distributions. Unraid
arrays are supported as one storage type, but Unraid is not required.

> [!IMPORTANT]
> Ubuntu Dashboard requires a **Linux Docker host**. Host telemetry depends on
> Linux interfaces such as `/proc`, `/sys`, block devices and the Docker socket.
> Docker Desktop on Windows or macOS cannot expose the same host information.

## Screenshots

![Ubuntu Dashboard overview](docs/screenshots/overview-ubuntu.png)

![Storage, Docker and host telemetry](docs/screenshots/overview-storage-docker.png)

## Highlights

- Live CPU, memory and network telemetry with a 500 ms dashboard tick rate
- Separate download and upload rates with blue/red history graphs
- Automatic default-route interface detection or explicit interfaces such as
  `bond0`, `eno1` or `eno1,eno2`
- SMART health, running state and temperatures for HDD, SATA SSD and NVMe drives
- Flexible storage views for Unraid arrays, ZFS pools/VDEVs, Linux md-RAID,
  LVM, Btrfs and standalone Linux disks
- Docker stack summary on the overview and individual containers in the
  Containers page
- Container start, stop and restart actions
- Green, yellow and red Docker health indicators and registry image-update hints
- Writable Data Browser with automatic SMB, NFS and mounted-data-root detection
- File owner, group, symbolic permissions and octal mode display
- Create, edit and delete folders and UTF-8 text/YAML/configuration files
- Interactive host terminal backed by a real SSH session
- Host process and classic log-file views
- Drag-and-drop overview layout with browser-local persistence
- Automatic distribution branding and responsive desktop/mobile layout
- English interface plus German, French, Spanish, Italian, Portuguese, Dutch
  and Polish language packs
- Persistent Discord webhook monitor for disk, container and system alerts
- Login sessions, HttpOnly cookies, CSRF protection and login rate limiting
- GitHub version check and a container health check

## Requirements

- A Linux server with Docker Engine
- Docker Compose v2, Arcane or another Compose-compatible manager
- `linux/amd64` or `linux/arm64`
- Port `8080` available, or another port configured through `DASHBOARD_PORT`
- A running SSH server if the integrated CLI should be used
- Outbound HTTPS access for GitHub version checks, registry update checks and
  optional Discord notifications

The published image is:

```text
ghcr.io/maomao63/ubuntu-dashboard:latest
```

## Quick start

Clone the repository, create the environment file and start the published
image:

```bash
git clone https://github.com/Maomao63/ubuntu-dashboard.git
cd ubuntu-dashboard
cp .env.example .env
nano .env
docker compose up -d
```

At minimum, replace the example password in `.env`:

```dotenv
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=use-a-long-unique-password
```

Open:

```text
http://SERVER-IP:8080
```

Check the deployment:

```bash
docker compose ps
docker compose logs --tail=100 ubuntu-dashboard
```

## Universal Docker Compose

The repository intentionally contains **one** deployment file. The same
`compose.yml` works with Docker Compose, Arcane and other Compose-compatible
Linux management interfaces:

```yaml
services:
  ubuntu-dashboard:
    image: ghcr.io/maomao63/ubuntu-dashboard:latest
    pull_policy: always
    container_name: ubuntu-dashboard
    restart: unless-stopped
    privileged: true
    ports:
      - "${DASHBOARD_PORT:-8080}:8080"
    environment:
      DASHBOARD_USER: "${DASHBOARD_USER:-admin}"
      DASHBOARD_PASSWORD: "${DASHBOARD_PASSWORD:-change-this-password-now}"
      ALLOW_DOCKER_ACTIONS: "${ALLOW_DOCKER_ACTIONS:-true}"
      SHARE_ROOTS: "${SHARE_ROOTS:-}"
      SSH_HOST: "${SSH_HOST:-auto}"
      SSH_PORT: "${SSH_PORT:-22}"
      NETWORK_INTERFACE: "${NETWORK_INTERFACE:-auto}"
      COOKIE_SECURE: "${COOKIE_SECURE:-false}"
      SESSION_TTL: "${SESSION_TTL:-43200}"
    volumes:
      - type: bind
        source: /
        target: /host
      - type: bind
        source: /proc/1/mounts
        target: /host-proc-mounts
        read_only: true
      - type: bind
        source: /proc/1/net/route
        target: /host-proc-net-route
        read_only: true
      - /sys:/host/sys:ro
      - /var/run/docker.sock:/var/run/docker.sock
      - ubuntu-dashboard-data:/data
    read_only: true
    tmpfs:
      - /tmp:size=16M,mode=1777

volumes:
  ubuntu-dashboard-data:
```

`pull_policy: always` makes a new deployment fetch the current `:latest`
image. The container itself remains read-only; only `/tmp`, the persistent
settings volume and the explicitly mounted host paths are writable.

## Arcane installation

1. Open **Projects** in Arcane and create a new Compose project.
2. Paste the contents of [`compose.yml`](compose.yml).
3. Add the values from [`.env.example`](.env.example) in the project's
   environment editor.
4. Set a strong `DASHBOARD_PASSWORD`.
5. If automatic network detection does not select the correct interface, set
   `NETWORK_INTERFACE`, for example to `bond0`.
6. Deploy the project and open `http://SERVER-IP:8080`.

Arcane must use the complete GHCR image name. If it tries to pull
`ubuntu-dashboard:latest` from Docker Hub, the project still contains an old
Compose definition. Replace it with:

```yaml
image: ghcr.io/maomao63/ubuntu-dashboard:latest
```

There is no `build:` section in the deployment Compose because Arcane should
pull the ready-to-use image instead of requiring the repository as a build
context.

## Environment configuration

Example `.env`:

```dotenv
DASHBOARD_PORT=8080
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=use-a-long-unique-password
ALLOW_DOCKER_ACTIONS=true
SHARE_ROOTS=
SSH_HOST=auto
SSH_PORT=22
NETWORK_INTERFACE=auto
COOKIE_SECURE=false
SESSION_TTL=43200
```

| Variable | Default | Description |
| --- | --- | --- |
| `DASHBOARD_PORT` | `8080` | Port exposed on the Docker host |
| `DASHBOARD_USER` | `admin` | Username used to create the initial account |
| `DASHBOARD_PASSWORD` | `change-this-password-now` | Initial account password; change before deployment |
| `ALLOW_DOCKER_ACTIONS` | `true` | Allows container start, stop and restart actions |
| `SHARE_ROOTS` | empty | Additional comma-separated Data Browser roots |
| `SSH_HOST` | `auto` | SSH target; `auto` uses the dashboard host name or IP |
| `SSH_PORT` | `22` | Host SSH port |
| `NETWORK_INTERFACE` | `auto` | Interface, bond or comma-separated interfaces to monitor |
| `COOKIE_SECURE` | `false` | Send the login cookie over HTTPS only |
| `SESSION_TTL` | `43200` | Login-session lifetime in seconds |

The environment credentials create the account only when no persistent account
exists yet. Changes made in **Settings** are stored as a PBKDF2 password hash in
the `ubuntu-dashboard-data` volume and take precedence after restarts.

## Host integration

The mounts in `compose.yml` have distinct purposes:

| Mount | Purpose | Access |
| --- | --- | --- |
| `/:/host` | Host identity, logs, data roots, filesystems and Data Browser | Read/write |
| `/proc/1/mounts` | Host mount table instead of the container mount namespace | Read-only |
| `/proc/1/net/route` | Host default-route detection | Read-only |
| `/sys:/host/sys` | Network counters, block devices and hardware telemetry | Read-only |
| `/var/run/docker.sock` | Docker inventory, health and container actions | Read/write |
| `ubuntu-dashboard-data:/data` | Account and Discord settings | Persistent |

`privileged: true` is required for broad physical-disk and SMART access across
different Linux hosts. Virtual machines may expose only virtual disks and no
temperature sensors; in that case missing temperature values are expected.

### Storage support

| Storage type | Dashboard representation |
| --- | --- |
| Unraid | Array, parity/data members and separate cache/pool devices |
| ZFS | Pool capacity, health, VDEVs and physical members |
| Linux md-RAID | RAID level, member devices and degraded state |
| LVM | Logical storage grouping, physical members and filesystem usage |
| Btrfs | Filesystem/pool, devices, capacity and health |
| Standalone disks | Device/model, type, capacity, health and mount usage |

SMART availability still depends on the host controller and drive passthrough.
Some USB bridges, hardware RAID controllers and virtual disks do not expose
SMART data to containers.

### Data Browser

The Data Browser automatically looks for:

- Samba share definitions
- NFS exports
- Mounted data disks below common Linux paths
- Distribution-appropriate directories such as `/home`, `/srv`, `/mnt`,
  `/media`, `/data` and `/storage`
- Additional roots configured with `SHARE_ROOTS`

It can create folders and text files, edit UTF-8 files up to 2 MB, and delete
files or complete folders after an explicit confirmation. Symbolic links are
not writable through the browser, and paths cannot escape the selected root.

### Host SSH terminal

The CLI tab opens a real SSH session to the host. It has the same permissions
as the supplied SSH account, including `sudo` only when that account is allowed
to use it. The SSH password is used for the active connection and is not stored
by the dashboard.

On Ubuntu/Debian, an SSH server can be enabled with:

```bash
sudo apt update
sudo apt install openssh-server
sudo systemctl enable --now ssh
```

For Fedora/RHEL-based hosts:

```bash
sudo dnf install openssh-server
sudo systemctl enable --now sshd
```

If `SSH_HOST=auto` cannot resolve the correct target behind a reverse proxy,
set the server address explicitly:

```dotenv
SSH_HOST=192.168.1.20
SSH_PORT=22
```

### Discord notifications

Open **Settings → Discord notifications**, paste a Discord webhook URL and
select the alert categories:

- disk health and temperature problems
- unhealthy, restarting or stopped Docker workloads
- general host/system problems

Mentions and the repeat interval are configurable, and a test button verifies
the webhook. The webhook secret is saved with file mode `0600` in the
persistent data volume and is never returned to the browser after saving.

## Updating

The Compose file tracks `:latest`. Pull and recreate the container to install a
new release:

```bash
docker compose pull
docker compose up -d --force-recreate
docker image prune -f
```

In Arcane, use **Pull** followed by **Redeploy**. A browser refresh alone does
not replace a running container image.

Dashboard settings survive updates because they are stored in the named
`ubuntu-dashboard-data` volume.

## Security

This dashboard intentionally has administrator-level host integrations:

- the Docker socket can control Docker workloads
- the writable `/host` mount powers the Data Browser
- privileged mode enables hardware and SMART discovery
- an authenticated SSH account can open a host shell

Treat access to the dashboard as access to the server itself.

- Change the example username and password before the first deployment.
- Keep the service on a trusted management network or behind a VPN.
- Do not expose port `8080` directly to the public internet.
- For remote access, use a reverse proxy with HTTPS and set
  `COOKIE_SECURE=true`.
- Add authentication and rate limiting at the reverse proxy as an additional
  layer.
- Use a dedicated SSH account with only the permissions it requires.
- Set `ALLOW_DOCKER_ACTIONS=false` if container control is not needed.
- Back up the `ubuntu-dashboard-data` volume with the rest of the host.

## Troubleshooting

### `pull access denied for ubuntu-dashboard`

The old short image name points Docker at Docker Hub. Use:

```yaml
image: ghcr.io/maomao63/ubuntu-dashboard:latest
```

No Docker Hub login is required for the public GHCR image.

### Network values are missing or show the wrong interface

Find the interface used by the default route:

```bash
ip route show default
```

Then set it explicitly, for example:

```dotenv
NETWORK_INTERFACE=bond0
```

Multiple interfaces can be aggregated with:

```dotenv
NETWORK_INTERFACE=eno1,eno2
```

### Disks or temperatures are missing

Verify that the current Compose is deployed with `privileged: true`, the root
host mount and `/sys:/host/sys:ro`. Also check whether the host itself exposes
SMART information:

```bash
sudo smartctl --scan
sudo smartctl -a /dev/sda
```

Virtual disks and some storage controllers do not expose temperatures.

### Docker remains unavailable

Verify the socket and container logs:

```bash
ls -l /var/run/docker.sock
docker compose logs --tail=200 ubuntu-dashboard
```

Rootless Docker uses a different socket path and requires the Compose mount to
be adapted to that host-specific socket.

### The SSH terminal cannot connect

Confirm that SSH listens on the configured host and port:

```bash
ss -lnt | grep ':22'
ssh USER@SERVER-IP
```

If direct SSH works but `auto` does not, set `SSH_HOST` explicitly.

### Changed `.env` credentials are ignored

The account saved in the persistent volume intentionally overrides the initial
environment values. Change the credentials in the dashboard Settings page.
Removing the named volume resets all persistent dashboard settings, including
the account and Discord webhook, and should only be used when that data is no
longer needed.

## Local development

Build the current source under a separate local tag:

```bash
git clone https://github.com/Maomao63/ubuntu-dashboard.git
cd ubuntu-dashboard
docker build --pull \
  --build-arg APP_VERSION=dev \
  -t ubuntu-dashboard:dev .
```

The application uses a Python standard-library backend and a dependency-light
HTML/CSS/JavaScript frontend. Xterm assets are bundled into the image during
the multi-stage build; no CDN is required at runtime.

Published images for `main` and version tags are built by GitHub Actions for
both AMD64 and ARM64 and pushed to GitHub Container Registry.
