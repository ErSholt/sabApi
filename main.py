import math
import sqlite3
import httpx
import asyncio
import os
import time
import re
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

os.makedirs(BLACKHOLE_DIR, exist_ok=True)

torbox_memory_cache: List[Dict] = []
last_api_fetch = 0

app = FastAPI()
templates = Jinja2Templates(directory="templates")


# --- DATABASE INITIALISIERUNG ---
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        # Wir bleiben bei 'history', wie gewünscht
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


# --- SABNZBD API ENDPUNKT ---
@app.api_route("/api", methods=["GET", "POST"])
async def sabnzbd_api(request: Request):
    params = dict(request.query_params)
    mode = params.get("mode")
    nzb_name = "Unknown NZB"
    if request.method == "POST":
        try:
            form_data = await request.form()
            for key, value in form_data.items():
                params[key] = value
            if "nzbfile" in form_data:
                upload = form_data["nzbfile"]
                if isinstance(upload, UploadFile):
                    nzb_name = upload.filename
                    content = await upload.read()
                    file_path = os.path.join(BLACKHOLE_DIR, nzb_name)
                    with open(file_path, "wb") as f:
                        f.write(content)
        except Exception as e:
            print(f"API Upload Error: {e}")

    if nzb_name == "Unknown NZB" and "name" in params:
        nzb_name = params["name"]

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

    if mode in ["addfile", "addurl"]:
        return JSONResponse({"status": True, "nzo_ids": ["proxy_added"]})
    return JSONResponse({"status": True, "version": "3.0.0"})


# --- CORE FUNKTION: TORBOX DATEN ---
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

    t_filtered = (
        [
            i
            for i in torbox_memory_cache
            if search_t.lower().strip() in i["name"].lower()
        ]
        if search_t
        else torbox_memory_cache
    )
    torbox_list = t_filtered[(p_t - 1) * ITEMS_PER_PAGE : p_t * ITEMS_PER_PAGE]
    total_t_pages = max(1, math.ceil(len(t_filtered) / ITEMS_PER_PAGE))

    altmount_data, total_h = [], 0
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

            raw_rows = [dict(row) for row in cursor.fetchall()]
            for log in raw_rows:
                raw_info = log.get("info", "")

                # --- INTELLIGENTE EXTRAKTION ---
                display_name = raw_info

                # 1. Falls es ein UploadFile-Objekt String ist, extrahiere filename='...'
                file_match = re.search(r"filename='([^']+)'", raw_info)
                if file_match:
                    display_name = file_match.group(1)

                # 2. Falls es ein Pfad oder nzb=... ist
                elif "nzb=" in display_name:
                    display_name = display_name.split("nzb=")[-1]
                elif "/" in display_name:
                    display_name = display_name.split("/")[-1]

                # 3. Aufräumen von Rest-Zeichen
                for char in ["'", "}", "]", "x-nzb", "{", '"', "Headers("]:
                    display_name = display_name.replace(char, "")

                log["display_name"] = display_name.strip()
            altmount_data = raw_rows
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
                        "request_log": altmount_data,
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
            "request_log": altmount_data,
            "page_t": p_t,
            "total_t_pages": total_t_pages,
            "page_h": p_h,
            "total_h_pages": total_h_pages,
            "total_history": total_h,
        },
    )


# --- LOGIN / LOGOUT ---
# ... (Login/Logout bleibt identisch)


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
