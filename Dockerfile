FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
COPY static ./static
COPY data ./data
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app
CMD ["python", "-m", "src.app"]
