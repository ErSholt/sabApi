import os, uuid, io, datetime, sqlite3, secrets, math
from fastapi import FastAPI, Request, Form, Depends, HTTPException, status
from fastapi.responses import Response, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request as StarletteRequest
import httpx

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# --- KONFIGURATION ---
BACKEND_URL = os.getenv("BACKEND_URL", "http://altmount:8080/sabnzbd/api")
BLACKHOLE_DIR = os.getenv("BLACKHOLE_DIR", "/blackhole")
DATABASE_DIR = os.getenv("DATABASE_DIR", "/config")
TORBOX_API_KEY = os.getenv("TORBOX_API_KEY", "")
PROXY_USER = os.getenv("PROXY_USER", "admin")
PROXY_PASS = os.getenv("PROXY_PASS", "password")
SESSION_TOKEN = secrets.token_hex(16) # Ändert sich bei Neustart -> Alle müssen sich neu einloggen
ITEMS_PER_PAGE = 10

os.makedirs(BLACKHOLE_DIR, exist_ok=True)
os.makedirs(DATABASE_DIR, exist_ok=True)
DB_PATH = os.path.join(DATABASE_DIR, "proxy_history.db")

# --- DATABASE ---
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, mode TEXT, info TEXT, status INTEGER)')
init_db()

def log_to_db(mode, info, status_code):
    try:
        timestamp = datetime.datetime.now().strftime("%H:%M:%S - %d.%m.")
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT INTO history (time, mode, info, status) VALUES (?, ?, ?, ?)", (timestamp, mode, info, status_code))
    except: pass

# --- AUTH LOGIK (COOKIE BASIERT) ---
def is_authenticated(request: Request):
    return request.cookies.get("session_id") == SESSION_TOKEN

# --- ROUTES ---
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login(response: Response, username: str = Form(...), password: str = Form(...)):
    if username == PROXY_USER and password == PROXY_PASS:
        response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(key="session_id", value=SESSION_TOKEN, httponly=True)
        return response
    return HTMLResponse("Falsche Daten. <a href='/login'>Zurück</a>", status_code=401)

@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login")
    response.delete_cookie("session_id")
    return response

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, page_h: int = 1, page_t: int = 1, content_only: int = 0):
    if not is_authenticated(request):
        if content_only:
            # Falls die Session abgelaufen ist, schicke einen Fehler-Status statt HTML
            return {"status": "unauthorized", "redirect": "/login"}
        return RedirectResponse(url="/login")
    
    # ... (Deine History & Torbox Logik hier) ...
    # (Stelle sicher, dass history_data und torbox_list hier berechnet werden)

    if content_only == 1:
        try:
            table_html = templates.get_template("table_snippet.html").render({
                "torbox_downloads": torbox_list, 
                "page_t": page_t, 
                "total_t_pages": total_t_pages,
                "torbox_error": torbox_error
            })
            # WICHTIG: Das muss ein Dictionary sein für JSON-Output
            return {
                "status": "success",
                "table_html": table_html, 
                "total_history": total_h
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # Normaler erster Seitenaufruf
    return templates.TemplateResponse("dashboard.html", {
        "request": request, "request_log": history_data, 
        "page_h": page_h, "total_h_pages": total_h_pages,
        "page_t": page_t, "total_t_pages": total_t_pages, 
        "torbox_downloads": torbox_list, "torbox_error": torbox_error
    })
# --- PROXY API (UNVERÄNDERT FÜR RADARR) ---
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
            if mode not in ["queue", "history"]: log_to_db(mode, log_info, resp.status_code)
            return Response(content=resp.content, status_code=resp.status_code, headers=dict(resp.headers))
        except Exception as e: return Response(content=str(e), status_code=500)