FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
COPY static ./static
RUN mkdir -p /app/data
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app
CMD ["python", "-m", "src.app"]
