FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data PORT=9106
WORKDIR /app
RUN groupadd --gid 10001 observatory && useradd --uid 10001 --gid observatory --no-create-home observatory && mkdir /data && chown observatory:observatory /data
COPY --chown=observatory:observatory dashboard/ /app/
USER 10001:10001
VOLUME ["/data"]
EXPOSE 9106
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9106/healthz',timeout=3)"]
CMD ["python", "server.py"]
