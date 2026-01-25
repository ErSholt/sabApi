import os
import shutil
import uuid
from fastapi import FastAPI, Request
from fastapi.responses import Response
import httpx

app = FastAPI()

BACKEND_URL = os.getenv("BACKEND_URL", "http://altmount:8080/sabnzbd")
BLACKHOLE_DIR = "/blackhole"
os.makedirs(BLACKHOLE_DIR, exist_ok=True)

@app.api_route("/api", methods=["GET", "POST"])
async def transparent_proxy(request: Request):
    params = dict(request.query_params)
    mode = params.get("mode")
    method = request.method
    
    # Den gesamten Body lesen, um ihn mehrfach verwenden zu können
    body = await request.body()
    content_type = request.headers.get("Content-Type")

    # 1. NZB-Backup (nur bei addfile & POST)
    if mode == "addfile" and method == "POST":
        try:
            # Wir speichern den rohen Body als .nzb, falls es kein Multipart ist,
            # oder wir suchen im Body nach dem Datei-Inhalt.
            # Um sicherzugehen, speichern wir den gesamten POST-Body als Backup.
            filename = f"{uuid.uuid4().hex[:6]}_upload.nzb"
            with open(os.path.join(BLACKHOLE_DIR, filename), "wb") as f:
                f.write(body)
            print(f"[Backup] Roher Body gesichert: {filename}")
        except Exception as e:
            print(f"[Backup Fehler] {e}")

    # 2. Transparente Weiterleitung
    async with httpx.AsyncClient(timeout=60.0) as client:
        # Wir kopieren fast alle Header, außer Host und Content-Length
        headers = {k: v for k, v in request.headers.items() 
                   if k.lower() not in ["host", "content-length", "connection"]}

        try:
            resp = await client.request(
                method=method,
                url=BACKEND_URL,
                params=params,
                content=body,
                headers=headers,
                follow_redirects=True
            )
            
            print(f"[Proxy] {mode} -> Status: {resp.status_code}")
            # Nur die ersten 100 Zeichen loggen, um Logs sauber zu halten
            print(f"[Proxy] Body: {resp.text[:100]}...")

            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers=dict(resp.headers)
            )
        except Exception as e:
            print(f"[Backend Error] {e}")
            return Response(content=str(e), status_code=500)