import os
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
    
    body = await request.body()
    
    # 1. Intelligentes NZB-Backup
    if mode == "addfile" and method == "POST":
        try:
            # Wir nutzen FastAPI's Form-Parser nur für das Backup im Hintergrund
            from fastapi.parsers import MultiPartParser
            
            # Wir simulieren einen Parser, um an die echte Datei zu kommen
            headers_dict = {k.lower(): v for k, v in request.headers.items()}
            content_type = headers_dict.get("content-type", "")
            
            if "multipart/form-data" in content_type:
                # Wir parsen den Body manuell für das Backup
                parser = MultiPartParser(request.headers, bytes_io := __import__("io").BytesIO(body))
                form = await parser.parse()
                
                # Wir suchen nach dem Feld 'nzbfile' oder 'name'
                file_item = form.get("nzbfile") or form.get("name")
                
                if hasattr(file_item, "filename"):
                    clean_name = file_item.filename
                    # Falls kein Name da ist, generieren wir einen
                    if not clean_name:
                        clean_name = f"{uuid.uuid4().hex[:6]}.nzb"
                    
                    # Speichern der sauberen Daten
                    dest_path = os.path.join(BLACKHOLE_DIR, clean_name)
                    with open(dest_path, "wb") as f:
                        f.write(file_item.file.read())
                    print(f"[Backup] Saubere NZB gespeichert: {clean_name}")
            else:
                # Fallback: Falls es kein Multipart ist, speichern wir den Body (selten)
                with open(os.path.join(BLACKHOLE_DIR, f"{uuid.uuid4().hex[:6]}.nzb"), "wb") as f:
                    f.write(body)
        except Exception as e:
            print(f"[Backup Fehler] Konnte NZB nicht sauber extrahieren: {e}")

    # 2. Transparente Weiterleitung (Unverändert, damit Altmount funktioniert)
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