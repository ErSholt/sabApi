# AGENTS.md – Projektanweisungen (sabApi)

Kurzüberblick
- FastAPI‑Service, der eine SABnzbd‑ähnliche API emuliert und NZB‑Uploads ins Blackhole‑Verzeichnis schreibt.
- Dashboard mit zwei Tabellen:
  - Torbox‑Downloads (über TorBox API, Cache in SQLite)
  - Altmount‑History (lokale Request‑Historie)
- Templates liegen in `templates/` und werden serverseitig via Jinja2 gerendert.

Projektstruktur
- `main.py`: FastAPI‑App, API‑Proxy, Dashboard, DB‑Initialisierung, TorBox‑Polling.
- `templates/`: `dashboard.html`, `torbox_table.html`, `altmount_table.html`, `login.html`.
- `requirements.txt`: Python‑Dependencies.
- `Dockerfile`: Container‑Build für `uvicorn main:app`.
- `seed_db.py`: Seed‑Script für eine **andere** DB (`proxy_history.db`) – aktuell nicht konsistent mit `main.py` (nutzt `proxy_altmount.db`).

Wichtige Umgebungsvariablen
- `TORBOX_API_KEY`: API‑Key für TorBox (optional, aber ohne liefert die Torbox‑Tabelle keine Daten).
- `DATABASE_DIR`: Ordner für die SQLite‑DB (Default `./`).
- `BLACKHOLE_DIR`: Zielverzeichnis für NZB‑Uploads (Default `/blackhole`). Muss existieren und schreibbar sein.
- `PROXY_USER`, `PROXY_PASS`: Login für Dashboard (Default `admin` / `password`).

Lokales Setup
1. `python -m venv .venv`
2. `.venv\Scripts\activate`
3. `pip install -r requirements.txt`
4. Umgebungsvariablen setzen (z. B. in `.env`).
5. Start: `uvicorn main:app --host 0.0.0.0 --port 8000`

Docker
- Build: `docker build -t sabapi .`
- Run (Beispiel): `docker run -p 8000:8000 -e TORBOX_API_KEY=... -e BLACKHOLE_DIR=/blackhole sabapi`
- Stelle sicher, dass `BLACKHOLE_DIR` im Container existiert und beschreibbar ist.

API‑Endpunkte (wichtigste)
- `POST /api`: SABnzbd‑kompatibler Upload‑Endpoint. Erwartet Datei‑Feld (`nzbfile`, `file`, `name` oder `nzb`), schreibt sie nach `BLACKHOLE_DIR`.
- `GET /api?mode=...&name=...`: Minimale SAB‑Antwort (für Sonarr/Radarr‑Validierung).
- `GET /`: Dashboard (Login erforderlich).
- `GET/POST /login`: Login‑Maske.
- `GET /logout`: Logout.

Datenbank
- `main.py` legt `proxy_altmount.db` (in `DATABASE_DIR`) an und nutzt Tabellen:
  - `history(id, info, time, mode, status)`
  - `torbox_cache(id, name, progress, state, updated_at)`
- `seed_db.py` schreibt in `proxy_history.db` → falls Seed‑Daten genutzt werden sollen, muss das Script an `proxy_altmount.db` angepasst oder `DB_PATH` vereinheitlicht werden.

Laufzeit‑Verhalten
- Beim Start werden `BLACKHOLE_DIR` und Schreibrechte geprüft.
- TorBox‑Daten werden in einem Loop alle 10s abgefragt (`/v1/api/usenet/mylist`) und in der DB sowie im RAM‑Cache gespeichert.
- Dashboard lädt Tabellen per AJAX via `/?content_only=1`.

Hinweise für Änderungen
- Wenn du die DB‑Struktur anpasst, immer `init_db()` und die Dashboard‑Queries in `main.py` aktualisieren.
- Upload‑Logik achtet auf verschiedene Feldnamen; Anpassungen bitte konsistent in `sabnzbd_api()` vornehmen.
- Bei Änderungen am UI: Templates liegen separat, keine JS‑Build‑Pipeline.
