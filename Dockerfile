FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY metis ./metis
COPY data ./data
CMD exec uvicorn metis.api:app --host 0.0.0.0 --port ${PORT:-8080}
