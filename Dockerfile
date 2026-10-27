FROM python:3.12-slim

WORKDIR /app
RUN python -m pip install --no-cache-dir 'psycopg[binary]>=3.2,<4'
COPY . /app

ENV PYTHONUNBUFFERED=1 SANDBOX_HOST=0.0.0.0 SANDBOX_PORT=8766
EXPOSE 8766 8767

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8766/health', timeout=2)"

CMD ["python3", "-m", "sandbox.server"]
