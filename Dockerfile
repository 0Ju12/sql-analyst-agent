FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --create-home agent
USER agent

# DB 파일은 이미지에 넣지 않고 실행할 때 연결한다:
#   docker run --rm -v "$PWD/data:/app/data" sql-analyst-agent tables
ENTRYPOINT ["sql-agent"]
