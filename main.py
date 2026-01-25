import os
import shutil
import uuid
from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import Response
import httpx

# Das ist die Instanz, die uvicorn sucht
app = FastAPI()

# Konfiguration
BACKEND_URL = os.getenv("BACKEND_URL", "http://altmount:8080/api")
BLACKHOLE_DIR = "/blackhole"

# Sicherstellen, dass der Ordner existiert
os.makedirs(BLACKHOLE_DIR, exist_ok=True)

@app.api_route("/api", methods=["GET", "POST"])
async def transparent_proxy(request: Request):
    params = dict(request.query_params)
    mode = params.get("mode")

    # 1. NZB abfangen bei 'addfile'
    if mode == "addfile" and request.method == "POST":
        form_data = await request.form()
        # Altmount nutzt oft 'nzbfile' oder 'name'
        nzb_file = form_data.get("nzbfile") or form_data.get("name")
        
        if isinstance(nzb_file, UploadFile):
            # Eindeutiger Name, um Überschreiben bei mehreren Arrs zu verhindern
            unique_name = f"{uuid.uuid4().hex[:6]}_{nzb_file.filename}"
            dest_path = os.path.join(BLACKHOLE_DIR, unique_name)
            
            with open(dest_path, "wb") as buffer:
                shutil.copyfileobj(nzb_file.file, buffer)
            
            await nzb_file.seek(0)
            print(f"[Backup] NZB gespeichert: {unique_name}")

    # 2. Transparente Weiterleitung
    async with httpx.AsyncClient(timeout=60.0) as client:
        # Wichtige Header für die Arrs & Altmount spiegeln
        proxy_headers = {k: v for k, v in request.headers.items() 
                         if k.lower() not in ["host", "content-length", "connection"]}

        data = None
        files = None
        
        if request.method == "POST":
            content_type = request.headers.get("Content-Type", "")
            if "multipart/form-data" in content_type:
                form = await request.form()
                data = {}
                files = {}
                for key, value in form.items():
                    if isinstance(value, UploadFile):
                        files[key] = (value.filename, await value.read(), value.content_type)
                    else:
                        data[key] = value
            else:
                data = await request.body()

        try:
            resp = await client.request(
                method=request.method,
                url=BACKEND_URL,
                params=params,
                data=data,
                files=files,
                headers=proxy_headers,
                follow_redirects=True
            )
            
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers=dict(resp.headers)
            )
        except Exception as e:
            print(f"[Error] Proxy Fehler: {e}")
            return Response(content='{"status": false, "error": "Proxy Connection Error"}', status_code=500)