FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv

# Node.js runs the demo HMS webhook receiver (integrations/hms-node, no npm dependencies).
RUN apt-get update && apt-get install -y --no-install-recommends nodejs && rm -rf /var/lib/apt/lists/*

# Dependencies first, so code changes don't reinstall them. The headless shell is enough for the bots and is
# much smaller than full Chromium, which matters on 512 MB instances.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev && playwright install --with-deps --only-shell chromium

COPY . .
RUN sed -i 's/\r$//' deploy/start.sh

EXPOSE 8000
CMD ["sh", "deploy/start.sh"]
