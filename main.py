import os
from re import search
import sqlite3
import datetime
import math
import httpx
import secrets
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
    page_h: int = 1, 
    page_t: int = 1, 
    content_only: int = 0, 
    filter_active: int = 1, 
    search_t: str = "", # Suche für Torbox
    search_h: str = ""  # Suche für History
):
    if not is_authenticated(request):
        if content_only: return JSONResponse({"status": "unauthorized", "redirect": "/login"})
        return RedirectResponse(url="/login")
    
    # Suchbegriffe säubern
    search_t_term = search_t.strip().lower()
    search_h_term = search_h.strip().lower()

    try:
        # --- 1. PROXY HISTORY LOGIK (SQL-Datenbank) ---
        offset_h = (page_h - 1) * ITEMS_PER_PAGE
        conditions = []
        
        if int(filter_active) == 1:
            conditions.append("(info != 'Request' AND mode NOT IN ('queue', 'status', 'history'))")
        
        if search_h_term:
            # Einfaches Escaping für die Suche in der DB
            safe_search = search_h_term.replace("'", "''")
            conditions.append(f"info LIKE '%{safe_search}%'")
        
        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

        with sqlite3.connect(DB_PATH) as conn:
            # Gesamtanzahl der (gefilterten) Einträge für History-Pagination
            total_h = conn.execute(f"SELECT COUNT(*) FROM history {where_clause}").fetchone()[0]
            total_h_pages = max(1, math.ceil(total_h / ITEMS_PER_PAGE))
            
            # Daten abrufen
            query = f"SELECT time, mode, info, status FROM history {where_clause} ORDER BY id DESC LIMIT ? OFFSET ?"
            cur = conn.execute(query, (ITEMS_PER_PAGE, offset_h))
            history_data = [{"time": r[0], "mode": r[1], "info": r[2], "status": r[3]} for r in cur.fetchall()]

        # --- 2. TORBOX USENET API LOGIK (API + Python-Filter) ---
        torbox_list, total_t_pages, torbox_error = [], 1, None
        
        if TORBOX_API_KEY:
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        "https://api.torbox.app/v1/api/usenet/mylist", 
                        headers={"Authorization": f"Bearer {TORBOX_API_KEY}"}, 
                        timeout=5.0
                    )
                    
                    if resp.status_code == 200:
                        all_data = resp.json().get("data", [])
                        if not isinstance(all_data, list): all_data = []

                        # Separater Filter nur für Torbox-Einträge
                        if search_t_term:
                            all_data = [
                                i for i in all_data 
                                if i.get("name") and search_t_term in str(i.get("name")).lower()
                            ]
                        
                        total_items_t = len(all_data)
                        total_t_pages = max(1, math.ceil(total_items_t / ITEMS_PER_PAGE))
                        
                        # Pagination für Torbox
                        start = (page_t - 1) * ITEMS_PER_PAGE
                        end = start + ITEMS_PER_PAGE
                        selected_data = all_data[start:end]

                        torbox_list = [
                            {
                                "name": i.get("name", "Unbekannt"), 
                                "progress": round(float(i.get("progress", 0)) * 100, 1), 
                                "state": i.get("download_state", "unknown")
                            } 
                            for i in selected_data
                        ]
                    else:
                        torbox_error = f"Torbox API Fehler: {resp.status_code}"
            except Exception as e:
                print(f"Torbox API Error: {e}")
                torbox_error = "Torbox API nicht erreichbar"

        # --- 3. AJAX REFRESH WEICHE ---
        if int(content_only) == 1:
            return JSONResponse({
                "status": "success",
                "table_html": templates.get_template("table_snippet.html").render({
                    "torbox_downloads": torbox_list, "page_t": page_t, 
                    "total_t_pages": total_t_pages, "torbox_error": torbox_error
                }),
                "history_html": templates.get_template("history_snippet.html").render({
                    "request_log": history_data, "page_h": page_h, "total_h_pages": total_h_pages
                }),
                "total_history": total_h
            })

        # --- 4. NORMALER SEITENAUFRUF ---
        return templates.TemplateResponse("dashboard.html", {
            "request": request, 
            "request_log": history_data, "page_h": page_h, "total_h_pages": total_h_pages,
            "page_t": page_t, "total_t_pages": total_t_pages, 
            "torbox_downloads": torbox_list, "torbox_error": torbox_error
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