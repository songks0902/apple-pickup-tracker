FROM python:3.12-slim
WORKDIR /app
COPY app.py ./
RUN useradd -m appuser && mkdir -p /app/data && chown -R appuser:appuser /app
USER appuser
ENV PORT=8080 CACHE_DIR=/app/data PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"
CMD ["python3","app.py"]
