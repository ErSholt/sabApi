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

    print(f"[Proxy] {method} mode={mode}")

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Header vorbereiten - WICHTIG: Content-Type bei Multipart NICHT kopieren!
        headers = {k: v for k, v in request.headers.items() 
                   if k.lower() not in ["host", "content-length", "connection", "content-type"]}

        # Spezialfall: Upload (addfile)
        if method == "POST" and "multipart/form-data" in request.headers.get("Content-Type", ""):
            form = await request.form()
            payload = {}
            files = [] # Liste statt Dict für httpx Multipart

            for key, value in form.items():
                if isinstance(value, UploadFile):
                    content = await value.read()
                    
                    # Backup im Blackhole
                    if mode == "addfile":
                        unique_name = f"{uuid.uuid4().hex[:6]}_{value.filename}"
                        with open(os.path.join(BLACKHOLE_DIR, unique_name), "wb") as f:
                            f.write(content)
                        print(f"[Backup] Gespeichert: {unique_name}")
                    
                    # WICHTIG: Den Original-Key (nzbfile oder name) beibehalten
                    files.append((key, (value.filename, content, value.content_type)))
                else:
                    payload[key] = value

            try:
                # Hier lassen wir httpx den Content-Type inkl. Boundary neu setzen
                resp = await client.post(BACKEND_URL, params=params, data=payload, files=files, headers=headers)
            except Exception as e:
                print(f"[Backend Error] {e}")
                return Response(content=str(e), status_code=500)

        # Standard-Weiterleitung für alles andere
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
                print(f"[Backend Error] {e}")
                return Response(content=str(e), status_code=500)

        print(f"[Backend] Status: {resp.status_code}")
        return Response(content=resp.content, status_code=resp.status_code, headers=dict(resp.headers))