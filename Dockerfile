FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY mcp_common/ mcp_common/

# Cloud Run は PORT 環境変数でポートを渡してくる
ENV PORT=8080
EXPOSE 8080

CMD ["python", "server.py"]
