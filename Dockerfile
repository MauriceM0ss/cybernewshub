# Base image pinned by digest for reproducible, tamper-evident builds.
# Refresh with: docker pull python:3.12-slim && docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim
FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

VOLUME ["/data"]

EXPOSE 8080

ENV DB_PATH=/data/feeds.db
# How often the background thread refreshes every feed, in minutes.
ENV REFRESH_MINUTES=30

# Liveness probe — hits the in-app /healthz route (which pings SQLite). Uses the
# stdlib so we don't add curl to the image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status==200 else 1)"]

CMD ["python", "app.py"]
