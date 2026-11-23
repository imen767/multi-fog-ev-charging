FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Correctif: le backend importe aussi le moteur de simulation.
COPY fog ./fog
COPY shared ./shared
COPY simulator ./simulator

# Correctif: initialiser chaque base Fog depuis les catalogues inclus.
COPY import_urban_ev.py bootstrap_db.py ./
COPY stations_catalog.csv piles_catalog.csv ./

EXPOSE 8000
CMD ["sh", "-c", "python bootstrap_db.py && exec uvicorn fog.api.main:app --host 0.0.0.0 --port 8000"]
