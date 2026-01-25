import os
import shutil
import uuid
from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import Response
import httpx

app = FastAPI()

# Konfiguration
BACKEND_URL = os.getenv("BACKEND_URL", "http://altmount:8080/sabnzbd")
BLACKHOLE_DIR = "/blackhole"

os.makedirs(BLACKHOLE_DIR, exist_ok=True)

@app.api_route("/api", methods=["GET", "POST"])
async def transparent_proxy(request: Request):
    params = dict(request.query_params)
    mode = params.get("mode")
    method = request.method

    print(f"[Proxy] Erhalten: {method} mode={mode}")

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Header vorbereiten (Host und Content-Length weglassen, httpx setzt diese neu)
        headers = {k: v for k, v in request.headers.items() 
                   if k.lower() not in ["host", "content-length", "connection"]}

        # SPEZIALFALL: POST mit Datei (addfile)
        if method == "POST" and "multipart/form-data" in request.headers.get("Content-Type", ""):
            form = await request.form()
            payload = {}
            files = {}

            for key, value in form.items():
                if isinstance(value, UploadFile):
                    # Dateiinhalt lesen
                    file_content = await value.read()
                    
                    # 1. Backup im Blackhole (nur bei mode=addfile)
                    if mode == "addfile":
                        try:
                            unique_name = f"{uuid.uuid4().hex[:6]}_{value.filename}"
                            dest_path = os.path.join(BLACKHOLE_DIR, unique_name)
                            with open(dest_path, "wb") as f:
                                f.write(file_content)
                            print(f"[Backup] NZB gespeichert: {unique_name}")
                        except Exception as e:
                            print(f"[Backup] Fehler: {e}")

                    # 2. Datei für den Forward vorbereiten
                    files[key] = (value.filename, file_content, value.content_type)
                else:
                    payload[key] = value

            # Request an Altmount senden
            try:
                resp = await client.post(BACKEND_URL, params=params, data=payload, files=files, headers=headers)
            except Exception as e:
                print(f"[Backend] Fehler beim POST Forward: {e}")
                return Response(content=f'{{"status": false, "error": "{str(e)}"}}', status_code=500)

        # STANDARD: Alles andere (GET, normales POST)
        else:
            try:
                body = await request.body()
                resp = await client.request(
                    method=method,
                    url=BACKEND_URL,
                    params=params,
                    content=body,
                    headers=headers
                )
            except Exception as e:
                print(f"[Backend] Fehler beim Forward: {e}")
                return Response(content=f'{{"status": false, "error": "{str(e)}"}}', status_code=500)

        print(f"[Backend] Altmount antwortete: {resp.status_code} für mode={mode}")
        
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=dict(resp.headers)
        )