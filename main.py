import os
import uuid
import io
import datetime
import sqlite3
import secrets
import math
from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.responses import Response, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from starlette.requests import Request as StarletteRequest
import httpx

app = FastAPI()
security = HTTPBasic()

# --- KONFIGURATION ---
BACKEND_URL = os.getenv("BACKEND_URL", "http://192.168.1.100:8080/api")
BLACKHOLE_DIR = os.getenv("BLACKHOLE_DIR", "/blackhole")
DATABASE_DIR = os.getenv("DATABASE_DIR", "/config")
TORBOX_API_KEY = os.getenv("TORBOX_API_KEY", "")
PROXY_USER = os.getenv("PROXY_USER", "admin")
PROXY_PASS = os.getenv("PROXY_PASS", "password")
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
    except Exception as e: print(f"DB Error: {e}")

# --- AUTH ---
def get_current_username(credentials: HTTPBasicCredentials = Depends(security)):
    if not (secrets.compare_digest(credentials.username, PROXY_USER) and 
            secrets.compare_digest(credentials.password, PROXY_PASS)):
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})
    return credentials.username

# --- DEBUG ROUTE (Für dein NPM Problem) ---
@app.get("/debug-headers")
async def debug_headers(request: Request):
    return {
        "headers": dict(request.headers),
        "auth_header_present": "authorization" in request.headers,
        "method": request.method,
        "url": str(request.url)
    }

# --- DASHBOARD ---
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, page_h: int = 1, page_t: int = 1, username: str = Depends(get_current_username)):
    # 1. History mit Pagination
    offset_h = (page_h - 1) * ITEMS_PER_PAGE
    history_data = []
    total_h_pages = 0
    
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("SELECT COUNT(*) FROM history")
        total_h_items = cur.fetchone()[0]
        total_h_pages = math.ceil(total_h_items / ITEMS_PER_PAGE)
        
        cur = conn.execute("SELECT time, mode, info, status FROM history ORDER BY id DESC LIMIT ? OFFSET ?", 
                           (ITEMS_PER_PAGE, offset_h))
        history_data = [{"time": r[0], "mode": r[1], "info": r[2], "status": r[3]} for r in cur.fetchall()]

    # 2. Torbox mit Pagination
    torbox_list = []
    torbox_error = None
    total_t_pages = 1
    if TORBOX_API_KEY:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get("https://api.torbox.app/v1/api/torrents/mylist", 
                                        headers={"Authorization": f"Bearer {TORBOX_API_KEY}"})
                if resp.status_code == 200:
                    all_data = resp.json().get("data", [])
                    total_t_items = len(all_data)
                    total_t_pages = math.ceil(total_t_items / ITEMS_PER_PAGE)
                    # Slicing für Pagination
                    start = (page_t - 1) * ITEMS_PER_PAGE
                    end = start + ITEMS_PER_PAGE
                    for item in all_data[start:end]:
                        torbox_list.append({
                            "name": item.get("name", "Unknown"),
                            "progress": round((item.get("progress", 0) * 100), 1),
                            "speed": f"{round(item.get('download_speed', 0) / 1024 / 1024, 2)} MB/s",
                            "state": item.get("download_state", "unknown"),
                            "download_finished": item.get("download_finished", False)
                        })
        except Exception: torbox_error = "Torbox API Connection failed"

    return templates.TemplateResponse("dashboard.html", {
        "request": request, "request_log": history_data,
        "page_h": page_h, "total_h_pages": max(1, total_h_pages),
        "page_t": page_t, "total_t_pages": max(1, total_t_pages),
        "torbox_downloads": torbox_list, "torbox_error": torbox_error
    })

# --- PROXY ---
@app.api_route("/api", methods=["GET", "POST"])
async def transparent_proxy(request: Request):
    params = dict(request.query_params)
    mode = params.get("mode", "unknown")
    body = await request.body()
    log_info = "Request"
    
    if mode == "addfile" and request.method == "POST":
        try:
            content_type = request.headers.get("content-type", "")
            if "multipart/form-data" in content_type:
                async def mock_receive(): return {"type": "http.request", "body": body, "more_body": False}
                temp_req = StarletteRequest(request.scope.copy(), receive=mock_receive)
                form = await temp_req.form()
                for _, file_item in form.items():
                    if hasattr(file_item, "filename") and file_item.filename:
                        log_info = file_item.filename
                        with open(os.path.join(BLACKHOLE_DIR, log_info), "wb") as f:
                            f.write(await file_item.read())
                        break
        except Exception as e: print(f"Extraction Error: {e}")

    async with httpx.AsyncClient(timeout=60.0) as client:
        headers = {k: v for k, v in request.headers.items() if k.lower() not in ["host", "content-length", "connection"]}
        try:
            resp = await client.request(method=request.method, url=BACKEND_URL, params=params, content=body, headers=headers)
            if mode not in ["queue", "history"]: log_to_db(mode, log_info, resp.status_code)
            return Response(content=resp.content, status_code=resp.status_code, headers=dict(resp.headers))
        except Exception as e: return Response(content=str(e), status_code=500)