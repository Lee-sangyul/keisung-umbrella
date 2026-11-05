FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/app/data
WORKDIR /app
RUN groupadd --gid 10001 umbrella && useradd --uid 10001 --gid umbrella --no-create-home umbrella
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY server.py school_year.py backup_db.py reset_db.py ./
COPY kiosk.html index.html print.html manifest.webmanifest sw.js ./
COPY assets ./assets
RUN mkdir -p /app/data && chown -R umbrella:umbrella /app/data
USER umbrella
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD ["python","-c","import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).read()"]
CMD ["uvicorn","server:app","--host","0.0.0.0","--port","8000","--workers","1","--no-server-header"]
