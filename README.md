# Ubuntu Control Dashboard

Ein leichtgewichtiges Web-Dashboard für Ubuntu Server – inspiriert von der
Informationsdichte von Unraid, aber ohne VM-Verwaltung. Es läuft als einzelner
Docker-Container und verwendet ausschließlich die Python-Standardbibliothek.

## Funktionen

- Live-Werte für CPU, RAM, Uptime und Netzwerk
- Temperaturen aus Thermal-Zones und hwmon für CPU, NVMe, Mainboard, GPU und drivetemp
- Docker-Container anzeigen, starten, stoppen und neu starten
- Docker-Healthstatus in Grün/Gelb/Rot und Registry-Prüfung auf neue Images
- Lokale Datenträger und Netzwerk-Mounts mit Belegung
- ext4/XFS, ZFS, Btrfs, md/LVM, Unraid shfs, NFS/SMB, Ceph und weitere Server-Dateisysteme
- Prozesse nach Speicherbedarf
- klassische Host-Logs aus `/var/log`
- schreibender Data Browser für Ordner sowie UTF-8-Text-, YAML- und Konfigurationsdateien
- Besitzer, Gruppe sowie symbolische und oktale Dateiberechtigungen
- frei sortierbare Dashboard-Karten mit gespeichertem Layout
- automatische Distributionserkennung und passendes Branding
- Live-Werte im 500-ms-Takt
- GitHub-Versionsprüfung und klickbarer SSH-Workflow für Host-Paketupdates
- englisches Basis-UI mit Deutsch, Französisch, Spanisch, Italienisch,
  Portugiesisch, Niederländisch und Polnisch
- vollständiges interaktives Host-Terminal über eine echte SSH-Sitzung
- responsives Web-UI für Desktop, Tablet und Smartphone
- Dashboard-Login mit zeitlich begrenzter Session, CSRF-Schutz und Rate-Limit
- eigener Settings-Bereich zum Ändern von Benutzername und Passwort
- Read-only-Dateisystem und Healthcheck im Container

## Start

```bash
cp .env.example .env
docker compose up -d --build
```

Danach ist das Dashboard unter `http://SERVER-IP:8080` erreichbar. Das Image
wird lokal als `ubuntu-dashboard:latest` gebaut. Die sichtbare App-Version
stammt direkt aus dem Image, damit eine alte Compose sie nicht überschreibt.

## In Arcane starten

Für Arcane die Datei `compose.arcane.yml` verwenden. Sie enthält keinen
Build-Block und zieht immer das veröffentlichte Multi-Arch-Image:

```text
ghcr.io/maomao63/ubuntu-dashboard:latest
```

In Arcane unter **Compose Projects** ein neues Projekt erstellen, den gesamten
Inhalt der Datei einfügen und starten. `pull_policy: always` sorgt dafür, dass
ein Redeploy stets das aktuelle `:latest`-Image lädt. Vor dem Start in Arcane
unbedingt `DASHBOARD_USER` und `DASHBOARD_PASSWORD` als Umgebungsvariablen auf
eigene, starke Werte setzen. Die in der Compose enthaltenen Fallback-Werte sind
nur dafür gedacht, einen ersten lokalen Start zu ermöglichen.

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
einen Reverse Proxy mit HTTPS verwenden und dann `COOKIE_SECURE=true` setzen.
Der Login erzeugt eine zufällige, nur per HttpOnly-Cookie zugängliche Session,
prüft schreibende Requests mit einem CSRF-Token und begrenzt Fehlversuche.
Das erste Konto wird aus `DASHBOARD_USER` und `DASHBOARD_PASSWORD` erstellt.
Spätere Änderungen im Settings-Tab werden als PBKDF2-Hash im Docker-Volume
`ubuntu-dashboard-data` gespeichert und bleiben bei Container-Updates erhalten.

Der Host wird für den Data Browser unter `/host` schreibbar eingebunden.
Erstellen, Bearbeiten und Löschen ist auf erkannte oder über `SHARE_ROOTS`
konfigurierte Wurzelverzeichnisse begrenzt. Löschvorgänge benötigen eine
zusätzliche Bestätigung. Mit `ALLOW_DOCKER_ACTIONS=false` werden nur die
Docker-Aktionen deaktiviert.

Freigaben werden automatisch aus Samba-Konfigurationen, NFS-Exports, echten
Datenträger-Mounts und distributionsüblichen Pfaden erkannt. Dabei werden unter
anderem Ubuntu, Debian, Fedora/Rocky, Arch/Manjaro und openSUSE berücksichtigt.
Zusätzliche Ordner können mit `SHARE_ROOTS=/data/media,/data/backups` angegeben
werden. Der Data Browser zeigt Namen, Ordnerstruktur, Größen und
Änderungszeiten, erstellt Ordner und Textdateien, bearbeitet Dateien bis 2 MB
und kann Dateien oder komplette Ordner nach Bestätigung löschen.

Die integrierte CLI öffnet eine echte SSH-Sitzung zum Host und kann damit alles,
was derselbe Benutzer bei einer normalen SSH-Anmeldung kann – einschließlich
`sudo`, sofern der Benutzer dafür berechtigt ist. Auf dem Host muss dazu ein
SSH-Server laufen und Passwort-/Keyboard-Interactive-Anmeldung zulassen. Das
Dashboard übernimmt bei `SSH_HOST=auto` automatisch die Server-IP aus der
aufgerufenen Dashboard-Adresse. Ziel und Port können über `SSH_HOST` und
`SSH_PORT` fest vorgegeben werden. Das SSH-Passwort
wird nur an den lokalen SSH-Prozess weitergereicht und weder geloggt noch
gespeichert.

## Version veröffentlichen

Für eine neue Version Build-Argument und Workflow-Tag ändern und
anschließend bauen:

```bash
docker compose build --pull
docker tag ubuntu-dashboard:latest ghcr.io/dein-name/ubuntu-dashboard:1.8.0
docker tag ubuntu-dashboard:latest ghcr.io/dein-name/ubuntu-dashboard:latest
docker push ghcr.io/dein-name/ubuntu-dashboard:1.8.0
docker push ghcr.io/dein-name/ubuntu-dashboard:latest
```
