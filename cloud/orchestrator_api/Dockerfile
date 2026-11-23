FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY cloud/orchestrator_api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY simulator/__init__.py /app/simulator/__init__.py
COPY simulator/orchestrator.py /app/simulator/orchestrator.py
COPY simulator/cooperative_orchestrator.py /app/simulator/cooperative_orchestrator.py
COPY cloud/orchestrator_api/main.py /app/main.py
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
