FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --no-install-project

COPY main.py ./
COPY src ./src
RUN uv sync

ENV HOST=0.0.0.0
ENV PORT=8765
EXPOSE 8765

CMD ["uv", "run", "main.py"]
