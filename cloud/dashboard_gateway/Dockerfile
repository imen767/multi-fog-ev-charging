FROM python:3.11-slim-bookworm
WORKDIR /app
COPY cloud/dashboard_gateway/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY cloud/dashboard_gateway/main.py /app/main.py
USER 65532:65532
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
