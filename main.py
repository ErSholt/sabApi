import os
import shutil
import uuid
from fastapi import FastAPI, Request, UploadFile
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

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Header filtern, aber Content-Type bei POST/Multipart NICHT mitschicken, 
        # da httpx diesen inklusive Boundary selbst generieren muss.
        headers = {k: v for k, v in request.headers.items() 
                   if k.lower() not in ["host", "content-length", "connection", "content-type"]}

        if method == "POST" and "multipart/form-data" in request.headers.get("Content-Type", "").lower():
            form = await request.form()
            data = {}
            files = {}

            for key, value in form.items():
                if isinstance(value, UploadFile):
                    content = await value.read()
                    
                    # Backup im Blackhole
                    if mode == "addfile":
                        unique_name = f"{uuid.uuid4().hex[:6]}_{value.filename}"
                        with open(os.path.join(BLACKHOLE_DIR, unique_name), "wb") as f:
                            f.write(content)
                        print(f"[Backup] Gespeichert: {unique_name}")
                    
                    # Hier ist die Änderung: Wir nutzen ein Dictionary für files
                    # httpx mappt dies dann korrekt auf den Feldnamen (key)
                    files[key] = (value.filename, content, value.content_type)
                else:
                    data[key] = value

            # Multipart-POST
            resp = await client.post(BACKEND_URL, params=params, data=data, files=files, headers=headers)
        else:
            # Alles andere (GET, JSON POST etc.)
            body = await request.body()
            resp = await client.request(method=method, url=BACKEND_URL, params=params, content=body, headers=headers)

        print(f"[Proxy] {mode} -> Status: {resp.status_code}")
        print(f"[Proxy] Body: {resp.text}")

        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type")
        )