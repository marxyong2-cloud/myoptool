FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY static/ ./static/

RUN mkdir -p /app/reports

ENV BENCH_NO_BROWSER=1
EXPOSE 8765

CMD ["python", "-u", "server.py"]
