FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# The recognition engine pulls in OpenCV, which links against these even when
# nothing is ever displayed — they are absent from the slim image. Without them
# `import cv2` raises, the engine is unavailable, and every export goes out as a
# plain image with no text layer: the one failure that looks like a bug in the
# export rather than a missing package. libxcb1 and libgl1 are asked for by
# name, libglib2.0-0 by way of libgthread.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libxcb1 libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

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
