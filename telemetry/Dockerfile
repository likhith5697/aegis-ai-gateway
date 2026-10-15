# The telemetry consumer: Redis Stream → InfluxDB. Small on purpose, no LiteLLM.
FROM python:3.13.16-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY consumer.py .

# -u: print logs immediately instead of buffering them
CMD ["python", "-u", "consumer.py"]
