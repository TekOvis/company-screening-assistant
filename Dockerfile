# Image for the demo server: "anthropic" or "mock" backend only.
# The claude_code backend (personal Claude subscription) must never run on a server.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first, as a cached layer: locked versions, no dev tools, no optional extras.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY third_party_licenses ./third_party_licenses
COPY src ./src
RUN uv sync --locked --no-dev

RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER app

# Safe defaults. Real settings (API key, access token, limits) come from an env file.
# PUBLIC_DEMO=true makes the app refuse to start without DEMO_ACCESS_TOKEN.
# FORWARDED_ALLOW_IPS="*" trusts X-Forwarded-For: publish the port on 127.0.0.1 only,
# so that the local reverse proxy is the only client that can reach the container.
ENV PATH="/app/.venv/bin:$PATH" \
    LLM_BACKEND=mock \
    PUBLIC_DEMO=true \
    COOKIE_SECURE=true \
    FORWARDED_ALLOW_IPS="*"

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"

CMD ["uvicorn", "screener.web:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
