# Schema mapping as its own service. Build from the repo root:
#   docker build -f mapping/Dockerfile -t schema-mapping .
#   docker run -p 8001:8001 --env-file .env schema-mapping
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY mapping/requirements.txt mapping/requirements.txt
RUN pip install --no-cache-dir -r mapping/requirements.txt
COPY mapping ./mapping
RUN useradd --create-home appuser && chown -R appuser /app
USER appuser
EXPOSE 8001
CMD ["uvicorn", "mapping.main:app", "--host", "0.0.0.0", "--port", "8001"]
