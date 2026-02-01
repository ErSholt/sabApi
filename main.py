import math
import sqlite3
import httpx
import asyncio
from fastapi import FastAPI, Request, Depends, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from typing import Optional

# Konfiguration (Hier deine Daten eintragen)
TORBOX_API_KEY = "DEIN_TORBOX_KEY"
DB_PATH = "nzb_proxy.db"
ITEMS_PER_PAGE = 10

app = FastAPI()
templates = Jinja2Templates(directory="templates")


# Beispiel Authentifizierung (sehr simpel gehalten)
def get_current_user(request: Request):
    user = request.cookies.get("user")
    if not user:
        return None
    return user


@app.get("/", response_class=HTMLResponse)
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
        return RedirectResponse(url="/login")

    search_t_term = search_t.lower().strip()
    search_h_term = search_h.lower().strip()

    try:
        # --- PARALLELE DATENABFRAGE (Optimiert für Speed) ---

        async def get_history():
            h_data, h_total = [], 0
            try:
                # Verbindung zur lokalen SQLite DB
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
            except Exception as e:
                print(f"DB Error: {e}")
            return h_data, h_total

        async def get_torbox():
            t_list, t_pages, t_err = [], 1, None
            if not TORBOX_API_KEY:
                return t_list, t_pages, "API Key fehlt"

            try:
                async with httpx.AsyncClient() as client:
                    # Timeout auf 3s begrenzt, damit das Dashboard nicht hängt
                    resp = await client.get(
                        "https://api.torbox.app/v1/api/usenet/mylist",
                        headers={"Authorization": f"Bearer {TORBOX_API_KEY}"},
                        timeout=3.0,
                    )

                    if resp.status_code == 200:
                        all_data = resp.json().get("data", [])
                        if not isinstance(all_data, list):
                            all_data = []

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
                                "name": i.get("name", "Unbekannt"),
                                "progress": round(float(i.get("progress", 0)) * 100, 1),
                                "state": i.get("download_state", "unknown")
                                .replace("_", " ")
                                .upper(),
                            }
                            for i in selected
                        ]
                    else:
                        t_err = f"Torbox API Fehler: {resp.status_code}"
            except Exception:
                t_err = "Torbox API Zeitüberschreitung"
            return t_list, t_pages, t_err

        # Beides gleichzeitig ausführen
        (history_data, total_h), (torbox_list, total_t_pages, torbox_error) = (
            await asyncio.gather(get_history(), get_torbox())
        )

        total_h_pages = max(1, math.ceil(total_h / ITEMS_PER_PAGE))

        # --- AJAX REFRESH WEICHE ---
        if int(content_only) == 1:
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

        # Vollständiger Seiten-Render
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
        return HTMLResponse(
            content=f"Ein kritischer Fehler ist aufgetreten: {e}", status_code=500
        )


# --- LOGIN / LOGOUT (Platzhalter) ---
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
