FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

RUN useradd --create-home --uid 1000 app
USER app

EXPOSE 8000
# --proxy-headers solo confía en X-Forwarded-For de las IPs de FORWARDED_ALLOW_IPS
# (variable que lee uvicorn). Sin ella, un cliente podría falsear su IP y saltarse el
# rate limit.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
