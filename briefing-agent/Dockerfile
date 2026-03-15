FROM python:3.12-slim
WORKDIR /app
RUN addgroup --system appuser && adduser --system --ingroup appuser appuser
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
USER appuser
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8085/healthz')" || exit 1
CMD ["python", "main.py"]
