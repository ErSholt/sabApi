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
BLACKHOLE_DIR = str(os.getenv("BLACKHOLE_DIR", "./blackhole"))
PROXY_USER = str(os.getenv("PROXY_USER", "admin"))
PROXY_PASS = str(os.getenv("PROXY_PASS", "password"))
ITEMS_PER_PAGE = 10

if not os.path.exists(BLACKHOLE_DIR):
    os.makedirs(BLACKHOLE_DIR, exist_ok=True)

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
        except Exception as e:
            print(f"Torbox Background Fetch Error: {e}")


# --- SABNZBD API (REPARIERTER UPLOAD & LOGGING) ---
@app.api_route("/api", methods=["GET", "POST"])
async def sabnzbd_api(request: Request):
    params = dict(request.query_params)
    mode = params.get("mode")
    nzb_name = "Unknown NZB"

    # 1. Namen aus den Query-Parametern holen (Radarr/Sonarr schicken oft ?name=...)
    if "name" in params:
        nzb_name = params["name"]

    if request.method == "POST":
        try:
            form_data = await request.form()
            # Falls im Formular ein 'name' Feld ist, dieses bevorzugen
            if "name" in form_data:
                nzb_name = str(form_data["name"])

            # NZB Datei speichern
            upload = form_data.get("nzbfile")
            if upload and hasattr(upload, "filename") and upload.filename:
                nzb_name = upload.filename
                file_path = os.path.join(BLACKHOLE_DIR, nzb_name)
                with open(file_path, "wb") as buffer:
                    shutil.copyfileobj(upload.file, buffer)
                print(f"Blackhole: {nzb_name} gespeichert.")
            else:
                # Falls wir den Content manuell aus dem Body fischen müssen
                print(f"Kein direkter File-Upload gefunden für: {nzb_name}")
        except Exception as e:
            print(f"API Fehler: {e}")

    # Logging in die DB
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO history (info, time, mode, status) VALUES (?, datetime('now','localtime'), ?, ?)",
                (str(nzb_name), str(mode), "200"),
            )
            conn.commit()
    except Exception as e:
        print(f"DB Log Fehler: {e}")

    if mode in ["addfile", "addurl"]:
        return JSONResponse({"status": True, "nzo_ids": ["proxy_added"]})
    return JSONResponse({"status": True, "version": "3.0.0"})


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
                info = log.get("info", "")
                # Starker Filter für Display Name
                f_match = re.search(r"filename='([^']+)'", info)
                name = (
                    f_match.group(1)
                    if f_match
                    else info.split("nzb=")[-1].split("/")[-1]
                )
                for c in [
                    "'",
                    "}",
                    "]",
                    "x-nzb",
                    "{",
                    '"',
                    "Headers(",
                    "UploadFile(filename=",
                ]:
                    name = name.replace(c, "")
                log["display_name"] = name.strip()
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
                "login.html", {"request": request, "error": "Ungültige Anmeldedaten"}
            )
        except Exception as e:
            return templates.TemplateResponse(
                "login.html", {"request": request, "error": str(e)}
            )
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login")
    response.delete_cookie("user")
    return response


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(fetch_torbox_to_db())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
