FROM python:3.12-slim
WORKDIR /app
COPY monitoring/exporter/main.py /app/main.py
USER 65532:65532
EXPOSE 8000
CMD ["python", "-u", "/app/main.py"]
