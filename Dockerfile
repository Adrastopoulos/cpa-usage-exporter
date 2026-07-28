FROM python:3.12-slim AS build

WORKDIR /build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1

COPY pyproject.toml README.md ./
COPY src ./src

# Build a wheel and install it into a self-contained prefix the runtime stage
# copies wholesale, so no build tooling reaches the final image.
RUN pip install --no-cache-dir hatchling \
    && pip wheel --no-deps --wheel-dir /wheels . \
    && pip install --no-cache-dir --prefix=/install /wheels/*.whl


FROM python:3.12-slim

LABEL org.opencontainers.image.title="cpa-usage-exporter" \
      org.opencontainers.image.description="Usage, cost and quota exporter for CLIProxyAPI" \
      org.opencontainers.image.licenses="Apache-2.0"

COPY --from=build /install /usr/local

RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin exporter
USER 10001

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

EXPOSE 9185

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('CPA_PROMETHEUS_PORT','9185')+'/metrics', timeout=4).status==200 else 1)"

ENTRYPOINT ["cpa-usage-exporter"]
