import os
import uuid
import io
import datetime
import sqlite3
import secrets
from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.responses import Response, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from starlette.requests import Request as StarletteRequest
import httpx

# --- KONFIGURATION ---
app = FastAPI()
security = HTTPBasic()

# Umgebungsvariablen
BACKEND_URL = os.getenv("BACKEND_URL", "http://altmount:8080/sabnzbd")
BLACKHOLE_DIR = os.getenv("BLACKHOLE_DIR", "/blackhole")
DATABASE_DIR = os.getenv("DATABASE_DIR", "/config")
TORBOX_API_KEY = os.getenv("TORBOX_API_KEY", "")
PROXY_USER = os.getenv("PROXY_USER", "admin")
PROXY_PASS = os.getenv("PROXY_PASS", "password")

# Ordner sicherstellen
os.makedirs(BLACKHOLE_DIR, exist_ok=True)
os.makedirs(DATABASE_DIR, exist_ok=True)

# Datenbank Pfad
DB_PATH = os.path.join(DATABASE_DIR, "proxy_history.db")
templates = Jinja2Templates(directory="templates")

# --- DATENBANK SETUP ---
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS history
                        (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                         time TEXT, 
                         mode TEXT, 
                         info TEXT, 
                         status INTEGER)''')
init_db()

def log_to_db(mode, info, status_code):
    try:
        timestamp = datetime.datetime.now().strftime("%H:%M:%S - %d.%m.")
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT INTO history (time, mode, info, status) VALUES (?, ?, ?, ?)", 
                         (timestamp, mode, info, status_code))
            conn.execute("DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 100)")
    except Exception as e:
        print(f"[DB Error] {e}")

# --- AUTHENTIFIZIERUNG ---
def get_current_username(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, PROXY_USER)
    correct_password = secrets.compare_digest(credentials.password, PROXY_PASS)
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Zugriff verweigert",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

# --- DASHBOARD ROUTE ---
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, username: str = Depends(get_current_username)):
    history_data = []
    total_reqs = 0
    total_nzbs = 0
    
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute("SELECT time, mode, info, status FROM history ORDER BY id DESC LIMIT 20")
        history_data = [{"time": r[0], "mode": r[1], "info": r[2], "status": r[3]} for r in cursor.fetchall()]
        total_reqs = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
        total_nzbs = conn.execute("SELECT COUNT(*) FROM history WHERE mode='addfile' AND status=200").fetchone()[0]

    torbox_downloads = []
    torbox_error = None
    if TORBOX_API_KEY:
        try:
            async with httpx.AsyncClient() as client:
                tb_resp = await client.get("https://api.torbox.app/v1/api/torrents/mylist", 
                                           headers={"Authorization": f"Bearer {TORBOX_API_KEY}"})
                if tb_resp.status_code == 200:
                    data = tb_resp.json().get("data", [])
                    for item in data:
                        torbox_downloads.append({
                            "name": item.get("name", "Unbekannt"),
                            "progress": round((item.get("progress", 0) * 100), 1),
                            "speed": f"{round(item.get('download_speed', 0) / 1024 / 1024, 2)} MB/s",
                            "state": item.get("download_state", "unknown"),
                            "download_finished": item.get("download_finished", False)
                        })
                else:
                    torbox_error = f"Torbox API Error: {tb_resp.status_code}"
        except Exception as e:
            torbox_error = f"Torbox Verbindung fehlgeschlagen."

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "request_log": history_data,
        "total_requests": total_reqs,
        "total_nzbs": total_nzbs,
        "torbox_downloads": torbox_downloads,
        "torbox_active_count": len(torbox_downloads),
        "torbox_error": torbox_error
    })

# --- PROXY LOGIK (STABIL & TRANSPARENT) ---
@app.api_route("/api", methods=["GET", "POST"])
async def transparent_proxy(request: Request):
    params = dict(request.query_params)
    mode = params.get("mode", "unknown")
    method = request.method
    body = await request.body()
    log_info = "Request"

    # NZB Extraction für Blackhole
    if mode == "addfile" and method == "POST":
        try:
            content_type = request.headers.get("content-type", "")
            if "multipart/form-data" in content_type:
                scope = request.scope.copy()
                async def mock_receive(): return {"type": "http.request", "body": body, "more_body": False}
                temp_request = StarletteRequest(scope, receive=mock_receive)
                form = await temp_request.form()
                for field_name, file_item in form.items():
                    if hasattr(file_item, "filename") and file_item.filename:
                        log_info = file_item.filename
                        content = await file_item.read()
                        with open(os.path.join(BLACKHOLE_DIR, log_info), "wb") as f:
                            f.write(content)
                        break
        except Exception as e:
            print(f"Extraction Error: {e}")

    # Forwarding zu Altmount
    async with httpx.AsyncClient(timeout=60.0) as client:
        headers = {k: v for k, v in request.headers.items() if k.lower() not in ["host", "content-length", "connection"]}
        try:
            resp = await client.request(method=method, url=BACKEND_URL, params=params, content=body, headers=headers, follow_redirects=True)
            if mode not in ["queue", "history", "version"]:
                 log_to_db(mode, log_info, resp.status_code)
            return Response(content=resp.content, status_code=resp.status_code, headers=dict(resp.headers))
        except Exception as e:
            return Response(content=str(e), status_code=500)