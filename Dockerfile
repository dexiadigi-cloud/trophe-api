# Trophe API — production image
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

# Application code
WORKDIR /srv/trophe/api
COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY api/ .

# The SQLite corpus ships as <100MB chunks (GitHub single-file limit);
# reassemble here. The app resolves it as ../data/trophe.db relative to
# api/ (see DB_PATH in app.py).
COPY data/dist/ /tmp/dist/
RUN mkdir -p ../data \
 && cat /tmp/dist/trophe.db.part-* > ../data/trophe.db \
 && rm -rf /tmp/dist

EXPOSE 8000

# TROPHE_API_KEY must be set in the host environment.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT} --proxy-headers"]
