import os
import uuid
import io
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
    
    # Wir lesen den Body einmal komplett
    body = await request.body()
    
    # 1. NZB-Backup Logik
    if mode == "addfile" and method == "POST":
        try:
            content_type = request.headers.get("content-type", "")
            if "multipart/form-data" in content_type:
                # Wir nutzen FastAPI's internes Form-Parsing, indem wir 
                # einen neuen Request mit dem bereits gelesenen Body simulieren
                from starlette.middleware import Middleware
                from starlette.requests import Request as StarletteRequest
                
                # Hilfs-Request erstellen, um die Form-Daten sauber zu parsen
                scope = request.scope.copy()
                receive = io.BytesIO(body)
                
                async def mock_receive():
                    return {"type": "http.request", "body": body, "more_body": False}
                
                temp_request = StarletteRequest(scope, receive=mock_receive)
                form = await temp_request.form()
                
                for field_name, file_item in form.items():
                    # Prüfen ob es ein Datei-Upload ist
                    if hasattr(file_item, "filename") and file_item.filename:
                        clean_name = file_item.filename
                        file_content = await file_item.read()
                        
                        dest_path = os.path.join(BLACKHOLE_DIR, clean_name)
                        with open(dest_path, "wb") as f:
                            f.write(file_content)
                        print(f"[Backup] Datei erfolgreich extrahiert: {clean_name}")
                        break
            else:
                # Fallback für Non-Multipart
                unique_name = f"{uuid.uuid4().hex[:6]}.nzb"
                with open(os.path.join(BLACKHOLE_DIR, unique_name), "wb") as f:
                    f.write(body)
        except Exception as e:
            print(f"[Backup Fehler] Extraktion fehlgeschlagen: {e}")

    # 2. Transparente Weiterleitung (Unverändert)
    async with httpx.AsyncClient(timeout=60.0) as client:
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
            
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers=dict(resp.headers)
            )
        except Exception as e:
            return Response(content=str(e), status_code=500)