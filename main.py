import os
import shutil
import uuid
from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import Response
import httpx

app = FastAPI()

# Konfiguration - Stelle sicher, dass diese URL von deinem Container aus erreichbar ist!
# Wenn Altmount auf dem gleichen Host läuft, nimm die LAN-IP (z.B. 192.168.1.50)
BACKEND_URL = os.getenv("BACKEND_URL", "http://altmount:8080/sabnzbd")
BLACKHOLE_DIR = "/blackhole"

os.makedirs(BLACKHOLE_DIR, exist_ok=True)

@app.api_route("/api", methods=["GET", "POST"])
async def transparent_proxy(request: Request):
    params = dict(request.query_params)
    mode = params.get("mode")
    method = request.method

    print(f"[Proxy] Erhalten: {method} mode={mode}")

    # 1. NZB-Backup Logik (nur bei addfile)
    if mode == "addfile" and method == "POST":
        try:
            form_data = await request.form()
            nzb_file = form_data.get("nzbfile") or form_data.get("name")
            if isinstance(nzb_file, UploadFile):
                unique_name = f"{uuid.uuid4().hex[:6]}_{nzb_file.filename}"
                dest_path = os.path.join(BLACKHOLE_DIR, unique_name)
                with open(dest_path, "wb") as buffer:
                    shutil.copyfileobj(nzb_file.file, buffer)
                await nzb_file.seek(0)
                print(f"[Backup] NZB erfolgreich gesichert: {unique_name}")
        except Exception as e:
            print(f"[Backup] Fehler beim Sichern der NZB: {e}")

    # 2. Weiterleitung an Altmount
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Header säubern (verhindert Konflikte mit Content-Length und Host)
        headers = {k: v for k, v in request.headers.items() 
                   if k.lower() not in ["host", "content-length", "connection"]}

        try:
            # Wir fangen den Body ab, um ihn weiterzuleiten
            body = await request.body()
            
            # Request an das Backend spiegeln
            resp = await client.request(
                method=method,
                url=BACKEND_URL,
                params=params,
                content=body,
                headers=headers,
                follow_redirects=True
            )

            print(f"[Backend] Altmount antwortete: {resp.status_code} für mode={mode}")
            
            # Die Antwort von Altmount 1:1 zurückgeben
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers=dict(resp.headers)
            )

        except httpx.ConnectError:
            print(f"[FEHLER] Verbindung zu Altmount unter {BACKEND_URL} fehlgeschlagen!")
            return Response(content='{"status": false, "error": "Altmount unreachable"}', status_code=502)
        except Exception as e:
            print(f"[FEHLER] Proxy-Fehler: {e}")
            return Response(content=f'{{"status": false, "error": "{str(e)}"}}', status_code=500)