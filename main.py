import math
import sqlite3
import httpx
import asyncio
import os
import time
import re
import shutil
from fastapi import (
    FastAPI,
    Request,
    Depends,
    Form,
    HTTPException,
    status,
    UploadFile,
    File,
)
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from typing import Optional, Any, List, Dict


# --- KONFIGURATION ---
TORBOX_API_KEY = str(os.getenv("TORBOX_API_KEY", ""))
DATABASE_DIR = str(os.getenv("DATABASE_DIR", "./"))
DB_PATH = os.path.join(DATABASE_DIR, "proxy_altmount.db")
BLACKHOLE_DIR = str(os.getenv("BLACKHOLE_DIR", "/blackhole"))
PROXY_USER = str(os.getenv("PROXY_USER", "admin"))
PROXY_PASS = str(os.getenv("PROXY_PASS", "password"))
ITEMS_PER_PAGE = 10
BACKEND_URL = str(os.getenv("BACKEND_URL", "http://altmount:8080/sabnzbd"))

# Startup Logging
print("\n" + "=" * 60)
print("PROXY STARTUP - KONFIGURATION")
print("=" * 60)
print(f"DATABASE_DIR: {DATABASE_DIR}")
print(f"DB_PATH: {DB_PATH}")
print(f"BLACKHOLE_DIR: {BLACKHOLE_DIR}")
print(f"PROXY_USER: {PROXY_USER}")
print(
    f"TORBOX_API_KEY: {'***' + TORBOX_API_KEY[-10:] if len(TORBOX_API_KEY) > 10 else 'NOT SET'}"
)
print("=" * 60 + "\n")

# Erstelle Blackhole-Verzeichnis
if not os.path.exists(BLACKHOLE_DIR):
    os.makedirs(BLACKHOLE_DIR, exist_ok=True)
    print(f"[STARTUP] Blackhole-Verzeichnis erstellt: {BLACKHOLE_DIR}")
else:
    print(f"[STARTUP] Blackhole-Verzeichnis existiert: {BLACKHOLE_DIR}")

# Ueberpruefe Schreibrechte
try:
    test_file = os.path.join(BLACKHOLE_DIR, ".write_test")
    with open(test_file, "w") as f:
        f.write("test")
    os.remove(test_file)
    print(f"[STARTUP] Schreibrechte OK: {BLACKHOLE_DIR}")
except Exception as e:
    print(f"[STARTUP ERROR] Keine Schreibrechte in {BLACKHOLE_DIR}: {e}")

print(f"[STARTUP] Absolute Pfad: {os.path.abspath(BLACKHOLE_DIR)}")
print()


torbox_memory_cache: List[Dict] = []
last_api_fetch = 0
cache_lock = asyncio.Lock()

app = FastAPI()
templates = Jinja2Templates(directory="templates")


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                info TEXT, time TEXT, mode TEXT, status TEXT
            )
        """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS torbox_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT, progress REAL, state TEXT, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """
        )
        conn.commit()


init_db()


def get_current_user(request: Request):
    return request.cookies.get("user")


async def fetch_torbox_to_db():
    global torbox_memory_cache, last_api_fetch
    async with cache_lock:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    "https://api.torbox.app/v1/api/usenet/mylist",
                    headers={"Authorization": f"Bearer {TORBOX_API_KEY}"},
                    timeout=5.0,
                )
                if resp.status_code == 200:
                    api_data = resp.json().get("data", [])
                    new_cache = []
                    with sqlite3.connect(DB_PATH) as conn:
                        cursor = conn.cursor()
                        cursor.execute("DELETE FROM torbox_cache")
                        for item in api_data:
                            name = item.get("name", "Unbekannt")
                            prog = round(float(item.get("progress", 0)) * 100, 1)
                            st = (
                                item.get("download_state", "unknown")
                                .replace("_", " ")
                                .upper()
                            )
                            cursor.execute(
                                "INSERT INTO torbox_cache (name, progress, state) VALUES (?, ?, ?)",
                                (name, prog, st),
                            )
                            new_cache.append(
                                {"name": name, "progress": prog, "state": st}
                            )
                        conn.commit()
                    torbox_memory_cache = new_cache
                    last_api_fetch = time.time()
                else:
                    torbox_memory_cache = []  # Leeren wenn API Fehler
        except Exception:
            torbox_memory_cache = []  # Leeren wenn API nicht erreichbar


# --- SABNZBD API (FORCE COPY & CLEAN NAME) ---
@app.api_route("/api", methods=["GET", "POST"])
async def sabnzbd_api(request: Request):
    print(f"\n{'='*60}")
    print(f"[API] Neue Anfrage: {request.method}")

    params = dict(request.query_params)
    mode = params.get("mode")
    final_name = "Unknown NZB"

    print(f"[API] Query Params: {params}")
    print(f"[API] Mode: {mode}")

    if request.method == "POST":
        try:
            form_data = await request.form()
            print(f"[API] Form Keys: {list(form_data.keys())}")

            # Debug: Zeige alle Form-Felder
            for key in form_data:
                value = form_data[key]
                # Prüfe auf UploadFile-Attribute statt isinstance
                if hasattr(value, "filename") and hasattr(value, "read"):
                    print(
                        f"[API] Field '{key}': UploadFile(filename='{value.filename}', size={getattr(value, 'size', 'unknown')})"
                    )
                else:
                    print(f"[API] Field '{key}': {type(value).__name__} = '{value}'")

            # Suche nach Upload-Datei - prüfe gängige Feldnamen
            upload_obj = None
            for field_name in ["nzbfile", "file", "name", "nzb"]:
                if field_name in form_data:
                    value = form_data[field_name]
                    # Prüfe auf UploadFile-Attribute statt isinstance
                    if hasattr(value, "filename") and hasattr(value, "read"):
                        upload_obj = value
                        print(f"[API] Upload gefunden in Feld: '{field_name}'")
                        break

            # Fallback: Suche in allen Feldern
            if not upload_obj:
                for key in form_data:
                    value = form_data[key]
                    # Prüfe auf UploadFile-Attribute statt isinstance
                    if hasattr(value, "filename") and hasattr(value, "read"):
                        upload_obj = value
                        print(f"[API] Upload gefunden in Feld: '{key}'")
                        break

            if upload_obj and hasattr(upload_obj, "filename") and upload_obj.filename:
                raw_filename = upload_obj.filename
                print(f"[API] Roher Filename: '{raw_filename}'")

                # Bereinigung des Dateinamens
                final_name = raw_filename.strip()
                final_name = re.sub(r'["\']', "", final_name)

                if not final_name.lower().endswith(".nzb"):
                    final_name += ".nzb"

                print(f"[API] Bereinigter Filename: '{final_name}'")

                # Kopiere Datei ins Blackhole-Verzeichnis
                file_path = os.path.join(BLACKHOLE_DIR, final_name)
                print(f"[API] Ziel-Pfad: {file_path}")
                print(f"[API] Blackhole-Dir existiert: {os.path.exists(BLACKHOLE_DIR)}")

                try:
                    await upload_obj.seek(0)
                    content = await upload_obj.read()
                    print(f"[API] Gelesene Bytes: {len(content)}")

                    with open(file_path, "wb") as buffer:
                        buffer.write(content)

                    # Überprüfe ob Datei existiert
                    if os.path.exists(file_path):
                        file_size = os.path.getsize(file_path)
                        print(f"[OK] NZB erfolgreich kopiert!")
                        print(f"[OK] Dateigröße: {file_size} Bytes")
                        print(f"[OK] Pfad: {file_path}")
                    else:
                        print(f"[ERROR] Datei wurde NICHT erstellt!")

                except Exception as copy_error:
                    print(f"[ERROR] Kopierfehler: {copy_error}")
                    import traceback

                    traceback.print_exc()
            else:
                print(f"[WARN] Kein UploadFile gefunden!")

                # Versuche filename aus 'name' Feld zu extrahieren (falls String)
                if "name" in form_data:
                    name_value = form_data["name"]
                    print(f"[API] 'name' Feld Typ: {type(name_value)}")
                    print(f"[API] 'name' Feld Wert: {name_value}")

                    # Wenn es ein String ist, extrahiere den Filename
                    if isinstance(name_value, str):
                        # Suche nach filename= Pattern
                        match = re.search(r'filename=["\']([^"\']+)["\']', name_value)
                        if match:
                            final_name = match.group(1).strip()
                            print(
                                f"[API] Filename aus String extrahiert: '{final_name}'"
                            )
                        else:
                            final_name = name_value.strip()

                # Bereinigung
                final_name = re.sub(r'["\']', "", final_name)
                if final_name and not final_name.lower().endswith(".nzb"):
                    final_name += ".nzb"

                print(f"[WARN] Verwende Name: '{final_name}'")

        except Exception as e:
            print(f"[ERROR] API POST Fehler: {e}")
            import traceback

            traceback.print_exc()

    elif request.method == "GET":
        if "name" in params:
            final_name = str(params["name"]).strip()
            final_name = re.sub(r'["\']', "", final_name)
            if not final_name.lower().endswith(".nzb"):
                final_name += ".nzb"
            print(f"[API] GET Request mit name: '{final_name}'")

    # Logging in die Datenbank
    print(f"[DB] Speichere in History: '{final_name}'")
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO history (info, time, mode, status) VALUES (?, datetime('now','localtime'), ?, ?)",
                (final_name, str(mode), "200"),
            )
            conn.commit()
        print(f"[DB] Erfolgreich gespeichert")
    except Exception as db_error:
        print(f"[ERROR] DB Fehler: {db_error}")

    print(f"{'='*60}\n")

    # Erweitertes Antwort-Format für Sonarr/Radarr Validierung
# --- TRANSPARENT PROXY / WEITERLEITUNG AN ALTMOUNT ---
    try:
        async with httpx.AsyncClient() as client:
            headers = dict(request.headers)
            headers.pop("host", None)
            headers.pop("content-length", None)

            if mode in ["addfile", "addurl"] and request.method == "POST":
                # Da wir den Body (content) oben schon mit upload_obj.read() gelesen haben,
                # müssen wir ihn hier manuell als 'files' oder 'data' mitschicken.
                # Wir schicken die Original-Parameter und die Datei an Altmount.
                
                # Wir bauen den Request für Altmount nach:
                # Da Sonarr meist multipart/form-data schickt, reichen wir es so weiter:
                files = {'nzbfile': (final_name, content)}
                data = dict(form_data) # Alle anderen Form-Felder (apikey, etc.)
                
                altmount_resp = await client.post(
                    BACKEND_URL,
                    params=params,
                    data=data,
                    files=files,
                    timeout=10.0
                )
            else:
                # Für get_config, queue etc. (GET Requests)
                altmount_resp = await client.get(
                    BACKEND_URL,
                    params=params,
                    headers=headers,
                    timeout=5.0
                )

            if altmount_resp.status_code == 200:
                print(f"[API] Weiterleitung an Altmount erfolgreich (Status 200)")
                return JSONResponse(content=altmount_resp.json())
            else:
                print(f"[API] Altmount antwortete mit Status: {altmount_resp.status_code}")
                
    except Exception as e:
        print(f"[PROXY ERROR] Weiterleitung zu Altmount ({BACKEND_URL}) fehlgeschlagen: {e}")

    # --- FALLBACK / EIGENE ANTWORT (Wenn Altmount nicht antwortet oder bei manuellem Upload) ---
    if mode in ["addfile", "addurl"]:
        return JSONResponse({"status": True, "nzo_ids": ["proxy_added"]})

    return JSONResponse(
        {
            "status": True,
            "version": "3.0.0",
            "queue": {
                "status": "Idle", "speed": "0", "size": "0 B", "sizeleft": "0 B",
                "slots": [], "noofslots": 0, "paused": False, "version": "3.0.0",
                "finish": 0, "cache_size": "0 B",
            },
            "server_stats": {
                "total_size": "0 B", "month_size": "0 B", "week_size": "0 B", "day_size": "0 B",
            },
        }
    )


# --- DASHBOARD ROUTE ---
@app.api_route("/", methods=["GET", "POST"], response_class=HTMLResponse)
async def dashboard(
    request: Request,
    page_t: int = 1,
    page_h: int = 1,
    filter_active: int = 1,
    search_t: str = "",
    search_h: str = "",
    content_only: int = 0,
    username: str = Depends(get_current_user),
):
    if not username:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    # Torbox Tabelle (wird nur gefüllt wenn API Daten liefert)
    t_filtered = (
        [
            i
            for i in torbox_memory_cache
            if search_t.lower().strip() in i["name"].lower()
        ]
        if search_t
        else torbox_memory_cache
    )
    total_t_pages = max(1, math.ceil(len(t_filtered) / ITEMS_PER_PAGE))
    torbox_list = t_filtered[(page_t - 1) * ITEMS_PER_PAGE : page_t * ITEMS_PER_PAGE]

    # Altmount Tabelle
    altmount_data, total_h = [], 0
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            h_query = "SELECT * FROM history WHERE 1=1"
            h_params = []
            if filter_active:
                h_query += " AND mode IN ('addfile', 'addurl')"
            if search_h:
                h_query += " AND info LIKE ?"
                h_params.append(f"%{search_h.strip()}%")

            cursor.execute(f"SELECT COUNT(*) FROM ({h_query})", h_params)
            total_h = cursor.fetchone()[0]
            cursor.execute(
                f"{h_query} ORDER BY id DESC LIMIT ? OFFSET ?",
                (*h_params, ITEMS_PER_PAGE, (page_h - 1) * ITEMS_PER_PAGE),
            )

            raw_rows = [dict(row) for row in cursor.fetchall()]
            for log in raw_rows:
                log["display_name"] = log.get("info", "Unknown NZB")
            altmount_data = raw_rows
    except:
        pass

    total_h_pages = max(1, math.ceil(total_h / ITEMS_PER_PAGE))

    if content_only == 1:
        return JSONResponse(
            {
                "status": "success",
                "table_html": templates.get_template("torbox_table.html").render(
                    {
                        "torbox_downloads": torbox_list,
                        "page_t": page_t,
                        "total_t_pages": total_t_pages,
                    }
                ),
                "history_html": templates.get_template("altmount_table.html").render(
                    {
                        "request_log": altmount_data,
                        "page_h": page_h,
                        "total_h_pages": total_h_pages,
                    }
                ),
                "total_history": total_h,
            }
        )

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "torbox_downloads": torbox_list,
            "request_log": altmount_data,
            "page_t": page_t,
            "total_t_pages": total_t_pages,
            "page_h": page_h,
            "total_h_pages": total_h_pages,
            "total_history": total_h,
        },
    )


# --- LOGIN / LOGOUT ---
@app.api_route("/login", methods=["GET", "POST"], response_class=HTMLResponse)
async def login(request: Request):
    if request.method == "POST":
        try:
            form_data = await request.form()
            if (
                str(form_data.get("username")) == PROXY_USER
                and str(form_data.get("password")) == PROXY_PASS
            ):
                response = RedirectResponse(
                    url="/", status_code=status.HTTP_303_SEE_OTHER
                )
                response.set_cookie(key="user", value=PROXY_USER)
                return response
            return templates.TemplateResponse(
                "login.html", {"request": request, "error": "Anmeldedaten falsch"}
            )
        except:
            return templates.TemplateResponse(
                "login.html", {"request": request, "error": "Systemfehler"}
            )
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login")
    response.delete_cookie("user")
    return response


async def torbox_update_loop():
    """
    Dieser Loop sorgt dafür, dass die torbox_table (linke Seite)
    regelmäßig mit frischen Daten von der TorBox API versorgt wird.
    """
    while True:
        await fetch_torbox_to_db()
        await asyncio.sleep(10)  # Aktualisierung alle 10 Sekunden


@app.on_event("startup")
async def startup_event():
    # Startet den dauerhaften Hintergrund-Loop für die TorBox-Tabelle
    asyncio.create_task(torbox_update_loop())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
