import math
import sqlite3
import httpx
import asyncio
import os
import time
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

# --- KONFIGURATION AUS DOCKER-UMGEBUNGSVARIABLEN ---
TORBOX_API_KEY = str(os.getenv("TORBOX_API_KEY", ""))
DATABASE_DIR = str(os.getenv("DATABASE_DIR", "./"))
DB_PATH = os.path.join(DATABASE_DIR, "proxy_history.db")
BLACKHOLE_DIR = str(os.getenv("BLACKHOLE_DIR", "./blackhole"))
PROXY_USER = str(os.getenv("PROXY_USER", "admin"))
PROXY_PASS = str(os.getenv("PROXY_PASS", "password"))
ITEMS_PER_PAGE = 10

# Sicherstellen, dass Verzeichnisse existieren
os.makedirs(BLACKHOLE_DIR, exist_ok=True)

# --- GLOBALER RAM-CACHE ---
torbox_memory_cache: List[Dict] = []
last_api_fetch = 0

app = FastAPI()
templates = Jinja2Templates(directory="templates")


# --- DATABASE INITIALISIERUNG ---
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        # History Tabelle (für das Dashboard)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                info TEXT,
                time TEXT,
                mode TEXT,
                status TEXT
            )
        """
        )
        # Cache Tabelle (für Torbox Daten)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS torbox_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                progress REAL,
                state TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """
        )
        conn.commit()


init_db()


def get_current_user(request: Request):
    return request.cookies.get("user")


# --- SABNZBD API ENDPUNKT (Fix für Radarr/Sonarr) ---
@app.api_route("/api", methods=["GET", "POST"])
async def sabnzbd_api(request: Request):
    params = dict(request.query_params)
    mode = params.get("mode")

    # Datei-Handling für Radarr/Sonarr Uploads
    nzb_name = "Unknown NZB"
    if request.method == "POST":
        try:
            form_data = await request.form()
            for key, value in form_data.items():
                params[key] = value

            # Falls eine Datei hochgeladen wird (nzbfile ist Standard bei SAB)
            if "nzbfile" in form_data:
                upload = form_data["nzbfile"]
                if isinstance(upload, UploadFile):
                    nzb_name = upload.filename
                    content = await upload.read()
                    # Speichern im Blackhole (optional, falls benötigt)
                    file_path = os.path.join(BLACKHOLE_DIR, nzb_name)
                    with open(file_path, "wb") as f:
                        f.write(content)
        except Exception as e:
            print(f"API Upload Error: {e}")

    # Fallback Name aus URL Params
    if nzb_name == "Unknown NZB" and "name" in params:
        nzb_name = params["name"]

    # 1. Logging in die Datenbank für das Dashboard
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO history (info, time, mode, status) VALUES (?, datetime('now','localtime'), ?, ?)",
                (str(nzb_name), str(mode), "200"),
            )
            conn.commit()
    except Exception as e:
        print(f"API DB Log Error: {e}")

    # 2. Response an Radarr/Sonarr (SABnzbd Format)
    if mode == "addfile" or mode == "addurl":
        return JSONResponse({"status": True, "nzo_ids": ["proxy_added"]})

    # Standard-Check für "Test Connection"
    return JSONResponse({"status": True, "version": "3.0.0"})


# --- CORE FUNKTION: TORBOX DATEN MANAGEMENT ---
async def refresh_torbox_data(force_api: bool = False):
    global torbox_memory_cache, last_api_fetch
    current_time = time.time()

    if force_api or (current_time - last_api_fetch > 15):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    "https://api.torbox.app/v1/api/usenet/mylist",
                    headers={"Authorization": f"Bearer {TORBOX_API_KEY}"},
                    timeout=4.0,
                )
                if resp.status_code == 200:
                    api_data = resp.json().get("data", [])
                    new_cache = []
                    with sqlite3.connect(DB_PATH) as conn:
                        cursor = conn.cursor()
                        cursor.execute("DELETE FROM torbox_cache")
                        for item in api_data:
                            name = item.get("name", "Unbekannt")
                            progress = round(float(item.get("progress", 0)) * 100, 1)
                            state = (
                                item.get("download_state", "unknown")
                                .replace("_", " ")
                                .upper()
                            )
                            cursor.execute(
                                "INSERT INTO torbox_cache (name, progress, state) VALUES (?, ?, ?)",
                                (name, progress, state),
                            )
                            new_cache.append(
                                {"name": name, "progress": progress, "state": state}
                            )
                        conn.commit()
                    torbox_memory_cache = new_cache
                    last_api_fetch = current_time
                    return
        except:
            pass

    if not torbox_memory_cache:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT name, progress, state FROM torbox_cache")
                torbox_memory_cache = [dict(row) for row in cursor.fetchall()]
        except:
            pass


# --- DASHBOARD ROUTE ---
@app.api_route("/", methods=["GET", "POST"], response_class=HTMLResponse)
async def dashboard(
    request: Request,
    page_t: Any = 1,
    page_h: Any = 1,
    filter_active: Any = 1,
    search_t: str = "",
    search_h: str = "",
    content_only: Any = 0,
    username: str = Depends(get_current_user),
):
    if not username:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    try:
        p_t, p_h = int(str(page_t)), int(str(page_h))
        c_only, f_active = int(str(content_only)), int(str(filter_active))
    except:
        p_t, p_h, c_only, f_active = 1, 1, 0, 1

    await refresh_torbox_data(force_api=(c_only == 1 and p_t == 1))

    t_filtered = torbox_memory_cache
    if search_t:
        t_filtered = [
            i for i in t_filtered if search_t.lower().strip() in i["name"].lower()
        ]

    total_t_pages = max(1, math.ceil(len(t_filtered) / ITEMS_PER_PAGE))
    torbox_list = t_filtered[(p_t - 1) * ITEMS_PER_PAGE : p_t * ITEMS_PER_PAGE]

    history_data, total_h = [], 0
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            h_query = "SELECT * FROM history WHERE 1=1"
            h_params = []
            if f_active:
                h_query += " AND mode IN ('addfile', 'addurl')"
            if search_h:
                h_query += " AND info LIKE ?"
                h_params.append(f"%{search_h.strip()}%")

            cursor.execute(f"SELECT COUNT(*) FROM ({h_query})", h_params)
            total_h = cursor.fetchone()[0]
            h_query += " ORDER BY id DESC LIMIT ? OFFSET ?"
            h_params.extend([ITEMS_PER_PAGE, (p_h - 1) * ITEMS_PER_PAGE])
            cursor.execute(h_query, h_params)
            history_data = [dict(row) for row in cursor.fetchall()]
    except:
        pass

    total_h_pages = max(1, math.ceil(total_h / ITEMS_PER_PAGE))

    if c_only == 1:
        return JSONResponse(
            {
                "status": "success",
                "table_html": templates.get_template("torbox_table.html").render(
                    {
                        "torbox_downloads": torbox_list,
                        "page_t": p_t,
                        "total_t_pages": total_t_pages,
                    }
                ),
                "history_html": templates.get_template("altmount_table.html").render(
                    {
                        "request_log": history_data,
                        "page_h": p_h,
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
            "request_log": history_data,
            "page_t": p_t,
            "total_t_pages": total_t_pages,
            "page_h": p_h,
            "total_h_pages": total_h_pages,
            "total_history": total_h,
        },
    )


# --- LOGIN / LOGOUT ---
@app.api_route("/login", methods=["GET", "POST"], response_class=HTMLResponse)
async def login(request: Request):
    if request.method == "POST":
        form_data = await request.form()
        if (
            str(form_data.get("username")) == PROXY_USER
            and str(form_data.get("password")) == PROXY_PASS
        ):
            response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
            response.set_cookie(key="user", value=PROXY_USER)
            return response
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "Ungültige Anmeldedaten"}
        )
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login")
    response.delete_cookie("user")
    return response


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
