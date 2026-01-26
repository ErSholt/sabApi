FROM python:3.11-slim

WORKDIR /app

# Erst die Requirements kopieren und installieren (für schnelleres Bauen)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# JETZT WICHTIG: Alles kopieren (inklusive des templates Ordners)
COPY . .

# Falls du sichergehen willst, dass der Ordner da ist:
RUN ls -R /app/templates

EXPOSE 8000

CMD ["uvicorn", "main.py:app", "--host", "0.0.0.0", "--port", "8000"]