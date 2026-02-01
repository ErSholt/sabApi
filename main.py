import os
from re import search
import sqlite3
import datetime
import math
import httpx
import secrets
import asyncio
from fastapi import FastAPI, Request, Form, status
from fastapi.responses import Response, HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request as StarletteRequest

app = FastAPI()

# --- KONFIGURATION ---
BACKEND_URL = os.getenv("BACKEND_URL", "http://altmount:8080/sabnzbd/api")
BLACKHOLE_DIR = os.getenv("BLACKHOLE_DIR", "/blackhole")
DATABASE_DIR = os.getenv("DATABASE_DIR", "/config")
TORBOX_API_KEY = os.getenv("TORBOX_API_KEY", "")
PROXY_USER = os.getenv("PROXY_USER", "admin")
PROXY_PASS = os.getenv("PROXY_PASS", "password")

# Fester Session-Token, damit man nach Container-Updates nicht ausgeloggt wird
SESSION_TOKEN = os.getenv("SESSION_TOKEN", "a1b2c3d4e5f6g7h8")
ITEMS_PER_PAGE = 10

os.makedirs(BLACKHOLE_DIR, exist_ok=True)
os.makedirs(DATABASE_DIR, exist_ok=True)
DB_PATH = os.path.join(DATABASE_DIR, "proxy_history.db")
templates = Jinja2Templates(directory="templates")

# --- DATABASE ---
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS history
                        (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                         time TEXT, mode TEXT, info TEXT, status INTEGER)''')
init_db()

def log_to_db(mode, info, status_code):
    try:
        timestamp = datetime.datetime.now().strftime("%H:%M:%S - %d.%m.")
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT INTO history (time, mode, info, status) VALUES (?, ?, ?, ?)", 
                         (timestamp, mode, info, status_code))
    except: pass

# --- AUTH ---
def is_authenticated(request: Request):
    return request.cookies.get("session_id") == SESSION_TOKEN

@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request, 
    page_t: int = 1, 
    page_h: int = 1, 
    filter_active: int = 1,
    search_t: str = "",
    search_h: str = "",
    content_only: int = 0,
    username: str = Depends(get_current_user)
):
    search_t_term = search_t.lower().strip()
    search_h_term = search_h.lower().strip()

    # Definition der Aufgaben für parallele Ausführung
    async def get_history():
        h_data, h_total = [], 0
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                query = "SELECT * FROM history WHERE 1=1"
                params = []
                if filter_active:
                    query += " AND mode = 'addfile'"
                if search_h_term:
                    query += " AND info LIKE ?"
                    params.append(f"%{search_h_term}%")
                
                cursor.execute(f"SELECT COUNT(*) FROM ({query})", params)
                h_total = cursor.fetchone()[0]
                query += " ORDER BY id DESC LIMIT ? OFFSET ?"
                params.extend([ITEMS_PER_PAGE, (page_h - 1) * ITEMS_PER_PAGE])
                cursor.execute(query, params)
                h_data = [dict(row) for row in cursor.fetchall()]
        except Exception: pass
        return h_data, h_total

    async def get_torbox():
        t_list, t_pages, t_err = [], 1, None
        if not TORBOX_API_KEY: return t_list, t_pages, t_err
        try:
            async with httpx.AsyncClient() as client:
                # Schnellerer Timeout, damit die Seite nicht hängt
                resp = await client.get(
                    "https://api.torbox.app/v1/api/usenet/mylist", 
                    headers={"Authorization": f"Bearer {TORBOX_API_KEY}"}, 
                    timeout=3.0 
                )
                if resp.status_code == 200:
                    all_data = resp.json().get("data", [])
                    if search_t_term:
                        all_data = [i for i in all_data if i.get("name") and search_t_term in str(i.get("name")).lower()]
                    
                    t_total = len(all_data)
                    t_pages = max(1, math.ceil(t_total / ITEMS_PER_PAGE))
                    start = (page_t - 1) * ITEMS_PER_PAGE
                    selected = all_data[start:start + ITEMS_PER_PAGE]
                    t_list = [{"name": i.get("name"), "progress": round(float(i.get("progress", 0)) * 100, 1), 
                                 "state": i.get("download_state", "unknown").replace("_", " ").upper()} for i in selected]
                else: t_err = f"API Error {resp.status_code}"
        except Exception: t_err = "Torbox Timeout"
        return t_list, t_pages, t_err

    # BEIDES GLEICHZEITIG STARTEN
    (history_data, total_h), (torbox_list, total_t_pages, torbox_error) = await asyncio.gather(
        get_history(), get_torbox()
    )

    total_h_pages = max(1, math.ceil(total_h / ITEMS_PER_PAGE))

    if int(content_only) == 1:
        return JSONResponse({
            "status": "success",
            "table_html": templates.get_template("torbox_table.html").render({
                "torbox_downloads": torbox_list, "page_t": page_t, "total_t_pages": total_t_pages, "torbox_error": torbox_error
            }),
            "history_html": templates.get_template("altmount_table.html").render({
                "request_log": history_data, "page_h": page_h, "total_h_pages": total_h_pages
            }),
            "total_history": total_h
        })

    return templates.TemplateResponse("dashboard.html", {
        "request": request, "torbox_downloads": torbox_list, "request_log": history_data,
        "page_t": page_t, "total_t_pages": total_t_pages, "page_h": page_h, 
        "total_h_pages": total_h_pages, "total_history": total_h, "torbox_error": torbox_error
    })

    except Exception as e:
        print(f"Server Error: {e}")
        if content_only: return JSONResponse({"status": "error", "message": str(e)}, status_code=500)
        return HTMLResponse(content=f"Ein Fehler ist aufgetreten: {e}", status_code=500)

# --- LOGIN / LOGOUT ---
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login(username: str = Form(...), password: str = Form(...)):
    if username == PROXY_USER and password == PROXY_PASS:
        response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(key="session_id", value=SESSION_TOKEN, httponly=True)
        return response
    return HTMLResponse("Login fehlgeschlagen. <a href='/login'>Zurück</a>", status_code=401)

@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login")
    response.delete_cookie("session_id")
    return response

# --- TRANSPARENT PROXY ---
@app.api_route("/api", methods=["GET", "POST"])
async def transparent_proxy(request: Request):
    params = dict(request.query_params)
    mode = params.get("mode", "unknown")
    body = await request.body()
    log_info = "Request"
    
    if mode == "addfile" and request.method == "POST":
        try:
            async def mock_receive(): return {"type": "http.request", "body": body, "more_body": False}
            temp_req = StarletteRequest(request.scope.copy(), receive=mock_receive)
            form = await temp_req.form()
            for _, file_item in form.items():
                if hasattr(file_item, "filename"):
                    log_info = file_item.filename
                    with open(os.path.join(BLACKHOLE_DIR, log_info), "wb") as f: f.write(await file_item.read())
                    break
        except: pass

    async with httpx.AsyncClient(timeout=60.0) as client:
        headers = {k: v for k, v in request.headers.items() if k.lower() not in ["host", "content-length", "connection"]}
        try:
            resp = await client.request(method=request.method, url=BACKEND_URL, params=params, content=body, headers=headers)
            # Loggen in DB (wird im Dashboard gefiltert wenn gewünscht)
            if mode not in ["queue", "history"]: log_to_db(mode, log_info, resp.status_code)
            return Response(content=resp.content, status_code=resp.status_code, headers=dict(resp.headers))
        except Exception as e: return Response(content=str(e), status_code=500)