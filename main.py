import math
import sqlite3
import httpx
import asyncio
import os  # Wichtig für Docker Umgebungsvariablen
from fastapi import FastAPI, Request, Depends, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from typing import Optional

# --- KONFIGURATION AUS DOCKER-UMGEBUNGSVARIABLEN ---
TORBOX_API_KEY = os.getenv("TORBOX_API_KEY", "")
DATABASE_DIR = os.getenv("DATABASE_DIR", "./")
DB_PATH = os.path.join(DATABASE_DIR, "nzb_proxy.db")
ITEMS_PER_PAGE = 10

app = FastAPI()
templates = Jinja2Templates(directory="templates")


# Beispiel Authentifizierung (Nutzt jetzt ebenfalls Docker-Envs)
def get_current_user(request: Request):
    user = request.cookies.get("user")
    if not user:
        return None
    return user


@app.middleware("http")
async def add_csp_header(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "script-src 'self' 'unsafe-inline' https://org.enteente.nl;"
    )
    return response


@app.api_route("/", methods=["GET", "POST"], response_class=HTMLResponse)
async def dashboard(
    request: Request,
    page_t: Optional[int] = 1,
    page_h: Optional[int] = 1,
    filter_active: Optional[int] = 1,
    search_t: str = "",
    search_h: str = "",
    content_only: Optional[int] = 0,
    username: str = Depends(get_current_user),
):
    if not username:
        return RedirectResponse(url="/login")

    # --- DATEN-EXTRAKTION AUS POST ODER GET ---
    if request.method == "POST":
        try:
            form_data = await request.form()

            # Wir holen den Wert und konvertieren ihn sicher zu einem String,
            # bevor wir int() darauf anwenden. Das beruhigt Pylance.
            raw_page_t = form_data.get("page_t")
            if raw_page_t is not None:
                page_t = int(str(raw_page_t))

            raw_page_h = form_data.get("page_h")
            if raw_page_h is not None:
                page_h = int(str(raw_page_h))

            raw_content_only = form_data.get("content_only")
            if raw_content_only is not None:
                content_only = int(str(raw_content_only))

            raw_filter_active = form_data.get("filter_active")
            if raw_filter_active is not None:
                filter_active = int(str(raw_filter_active))

            # Bei Strings ist es einfacher
            search_t = str(form_data.get("search_t", ""))
            search_h = str(form_data.get("search_h", ""))
        except (ValueError, TypeError) as e:
            print(f"Form conversion error: {e}")

    try:
        page_t = int(page_t) if page_t else 1
        page_h = int(page_h) if page_h else 1
        content_only = int(content_only) if content_only else 0
        filter_active = int(filter_active) if filter_active else 0
    except:
        page_t, page_h, content_only, filter_active = 1, 1, 0, 1

    search_t_term = search_t.lower().strip() if search_t else ""
    search_h_term = search_h.lower().strip() if search_h else ""

    try:

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
            except:
                pass
            return h_data, h_total

        async def get_torbox():
            t_list, t_pages, t_err = [], 1, None
            if not TORBOX_API_KEY:
                return t_list, t_pages, "Key fehlt"
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        "https://api.torbox.app/v1/api/usenet/mylist",
                        headers={"Authorization": f"Bearer {TORBOX_API_KEY}"},
                        timeout=3.0,
                    )
                    if resp.status_code == 200:
                        all_data = resp.json().get("data", [])
                        if search_t_term:
                            all_data = [
                                i
                                for i in all_data
                                if i.get("name")
                                and search_t_term in str(i.get("name")).lower()
                            ]

                        t_total = len(all_data)
                        t_pages = max(1, math.ceil(t_total / ITEMS_PER_PAGE))
                        start = (page_t - 1) * ITEMS_PER_PAGE
                        selected = all_data[start : start + ITEMS_PER_PAGE]
                        t_list = [
                            {
                                "name": i.get("name"),
                                "progress": round(float(i.get("progress", 0)) * 100, 1),
                                "state": i.get("download_state", "unknown")
                                .replace("_", " ")
                                .upper(),
                            }
                            for i in selected
                        ]
                    else:
                        t_err = f"API Error {resp.status_code}"
            except:
                t_err = "Torbox Timeout"
            return t_list, t_pages, t_err

        (history_data, total_h), (torbox_list, total_t_pages, torbox_error) = (
            await asyncio.gather(get_history(), get_torbox())
        )

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
                            "torbox_error": torbox_error,
                        }
                    ),
                    "history_html": templates.get_template(
                        "altmount_table.html"
                    ).render(
                        {
                            "request_log": history_data,
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
                "request_log": history_data,
                "page_t": page_t,
                "total_t_pages": total_t_pages,
                "page_h": page_h,
                "total_h_pages": total_h_pages,
                "total_history": total_h,
                "torbox_error": torbox_error,
            },
        )

    except Exception as e:
        print(f"Server Error: {e}")
        if content_only:
            return JSONResponse({"status": "error", "message": str(e)}, status_code=500)
        return HTMLResponse(content=f"Fehler: {e}", status_code=500)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login")
    response.delete_cookie("user")
    return response


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
