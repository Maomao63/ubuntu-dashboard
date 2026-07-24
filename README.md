# Ubuntu Control Dashboard

Ein leichtgewichtiges Web-Dashboard für Ubuntu Server – inspiriert von der
Informationsdichte von Unraid, aber ohne VM-Verwaltung. Es läuft als einzelner
Docker-Container und verwendet ausschließlich die Python-Standardbibliothek.

## Funktionen

- Live-Werte für CPU, RAM, Load, Uptime und Netzwerk
- Temperaturen, sofern der Host sie unter `/sys/class/thermal` bereitstellt
- Docker-Container anzeigen, starten, stoppen und neu starten
- Lokale Datenträger und Netzwerk-Mounts mit Belegung
- Prozesse nach Speicherbedarf
- klassische Host-Logs aus `/var/log`
- responsives Web-UI für Desktop, Tablet und Smartphone
- optionale HTTP-Basic-Anmeldung
- Read-only-Dateisystem und Healthcheck im Container

## Start

```bash
cp .env.example .env
docker compose up -d --build
```

Danach ist das Dashboard unter `http://SERVER-IP:8080` erreichbar. Das Image
wird lokal als `ubuntu-dashboard:latest` gebaut. Die sichtbare App-Version ist
in `compose.yml` sowohl als Build-Argument als auch als Umgebungsvariable
eingetragen.

## In Arcane starten

Für Arcane die Datei `compose.arcane.yml` verwenden. Sie enthält keinen lokalen
Build und zieht das fertige Multi-Arch-Image aus der GitHub Container Registry:

```yaml
services:
  ubuntu-dashboard:
    image: ghcr.io/maomao63/ubuntu-dashboard:latest
    pull_policy: always
    container_name: ubuntu-dashboard
    restart: unless-stopped
    ports:
      - "8080:8080"
    environment:
      APP_VERSION: "1.0.0"
      DASHBOARD_USER: ""
      DASHBOARD_PASSWORD: ""
      ALLOW_DOCKER_ACTIONS: "true"
    volumes:
      - /:/host:ro
      - /var/run/docker.sock:/var/run/docker.sock
    security_opt:
      - no-new-privileges:true
    read_only: true
    tmpfs:
      - /tmp:size=16M,mode=1777
```

In Arcane unter **Compose Projects** ein neues Projekt erstellen, den Inhalt
einfügen und starten. Da das GitHub-Repository privat ist, muss
`ghcr.io` zuvor in Arcane als Registry hinterlegt werden. Als Benutzer dient
der GitHub-Name; als Passwort wird ein Personal Access Token mit mindestens
Leserechten für Packages verwendet.

Wer das GHCR-Package später öffentlich schaltet, kann die Registry-Anmeldung
weglassen. Für eine Anmeldung am Dashboard selbst werden `DASHBOARD_USER` und
`DASHBOARD_PASSWORD` direkt in der Compose gesetzt.

## Bereits gebautes Image verwenden

Wenn das Image später in einer Registry liegt, kann der `build:`-Block aus der
Compose-Datei entfernt und die Image-Zeile beispielsweise so gesetzt werden:

```yaml
image: ghcr.io/dein-name/ubuntu-dashboard:latest
```

## Sicherheit

Der Docker-Socket erlaubt dem Dashboard bewusst die Verwaltung von Containern
und ist damit ein privilegierter Zugriff auf den Docker-Dienst. Das Dashboard
sollte nicht direkt ins Internet gestellt werden. Im Heimnetz mindestens
`DASHBOARD_USER` und `DASHBOARD_PASSWORD` in `.env` setzen; für externen Zugriff
einen Reverse Proxy mit HTTPS und zusätzlicher Authentifizierung verwenden.

Der Host wird unter `/host` nur lesbar eingebunden. Schreibende Aktionen gibt
es ausschließlich über die drei freigegebenen Docker-Endpunkte. Mit
`ALLOW_DOCKER_ACTIONS=false` wird die Oberfläche effektiv zum Read-only
Monitoring.

## Version veröffentlichen

Für eine neue Version beide Vorkommen von `1.0.0` in `compose.yml` ändern und
anschließend bauen:

```bash
docker compose build --pull
docker tag ubuntu-dashboard:latest ghcr.io/dein-name/ubuntu-dashboard:1.0.0
docker tag ubuntu-dashboard:latest ghcr.io/dein-name/ubuntu-dashboard:latest
docker push ghcr.io/dein-name/ubuntu-dashboard:1.0.0
docker push ghcr.io/dein-name/ubuntu-dashboard:latest
```
