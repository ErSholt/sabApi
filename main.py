import math
import sqlite3
import httpx
import asyncio
import os
import time
from fastapi import FastAPI, Request, Depends, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from typing import Optional, Any, List, Dict

# --- KONFIGURATION AUS DOCKER-UMGEBUNGSVARIABLEN ---
TORBOX_API_KEY = str(os.getenv("TORBOX_API_KEY", ""))
DATABASE_DIR = str(os.getenv("DATABASE_DIR", "./"))
DB_PATH = os.path.join(DATABASE_DIR, "proxy_history.db")
PROXY_USER = str(os.getenv("PROXY_USER", "admin"))
PROXY_PASS = str(os.getenv("PROXY_PASS", "password"))
ITEMS_PER_PAGE = 10

# --- GLOBALER RAM-CACHE (für die Millisekunden-Reaktion) ---
torbox_memory_cache: List[Dict] = []
last_api_fetch = 0

app = FastAPI()
templates = Jinja2Templates(directory="templates")


# --- DATABASE INITIALISIERUNG ---
def init_db():
    """Erstellt die Cache-Tabelle, falls sie nicht existiert."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
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


@app.middleware("http")
async def add_csp_header(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "script-src 'self' 'unsafe-inline' https://org.enteente.nl;"
    )
    return response


# --- CORE FUNKTION: TORBOX DATEN MANAGEMENT ---
async def refresh_torbox_data(force_api: bool = False):
    global torbox_memory_cache, last_api_fetch
    current_time = time.time()

    # 1. API Abfrage nur alle 15 Sekunden oder wenn erzwungen
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
                        # Alten Cache in DB leeren
                        cursor.execute("DELETE FROM torbox_cache")

                        for item in api_data:
                            name = item.get("name", "Unbekannt")
                            progress = round(float(item.get("progress", 0)) * 100, 1)
                            state = (
                                item.get("download_state", "unknown")
                                .replace("_", " ")
                                .upper()
                            )

                            # In DB speichern
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
        except Exception as e:
            print(f"Torbox API Error: {e}")

    # 2. Fallback: Wenn API nicht läuft oder kein Update nötig, aus DB laden (falls RAM leer)
    if not torbox_memory_cache:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT name, progress, state FROM torbox_cache")
                torbox_memory_cache = [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            print(f"Cache Load Error: {e}")


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

    # Typ-Konvertierung
    try:
        p_t, p_h = int(str(page_t)), int(str(page_h))
        c_only, f_active = int(str(content_only)), int(str(filter_active))
    except:
        p_t, p_h, c_only, f_active = 1, 1, 0, 1

    # Nur beim Auto-Refresh (content_only=1 und erste Seiten) die API triggern
    should_fetch_api = c_only == 1 and p_t == 1 and p_h == 1
    await refresh_torbox_data(force_api=should_fetch_api)

    # --- TORBOX LOKAL (RAM) FILTERN ---
    t_filtered = torbox_memory_cache
    if search_t:
        term = search_t.lower().strip()
        t_filtered = [i for i in t_filtered if term in i["name"].lower()]

    total_t_pages = max(1, math.ceil(len(t_filtered) / ITEMS_PER_PAGE))
    torbox_list = t_filtered[(p_t - 1) * ITEMS_PER_PAGE : p_t * ITEMS_PER_PAGE]

    # --- HISTORY LOKAL (DB) ---
    history_data, total_h = [], 0
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            h_query = "SELECT * FROM history WHERE 1=1"
            h_params = []
            if f_active:
                h_query += " AND mode = 'addfile'"
            if search_h:
                h_query += " AND info LIKE ?"
                h_params.append(f"%{search_h.strip()}%")

            cursor.execute(f"SELECT COUNT(*) FROM ({h_query})", h_params)
            total_h = cursor.fetchone()[0]

            h_query += " ORDER BY id DESC LIMIT ? OFFSET ?"
            h_params.extend([ITEMS_PER_PAGE, (p_h - 1) * ITEMS_PER_PAGE])
            cursor.execute(h_query, h_params)
            history_data = [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        print(f"DB History Error: {e}")

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
