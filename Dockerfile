FROM python:3.13.13-slim-bookworm@sha256:355bfa66770995d7e9a0da4b3473b44d0cb451f6b56f5615ad9c39e3c4eca03f

ARG SCHEMABRIDGE_RELEASE_REF=release-ref-not-supplied

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SCHEMABRIDGE_ENVIRONMENT=hosted-demo \
    SCHEMABRIDGE_AUTH_MODE=local-demo \
    SCHEMABRIDGE_CATALOG_MODE=recorded \
    SCHEMABRIDGE_PUBLICATION_MODE=fake \
    SCHEMABRIDGE_JUDGE_EXECUTION=recorded \
    SCHEMABRIDGE_LOCAL_WORKSPACE=public-judge-demo \
    SCHEMABRIDGE_LOCAL_ROLES='["analyst","publisher"]' \
    SCHEMABRIDGE_DRAFT_STORE_PATH=/tmp/schemabridge/schemabridge.db \
    SCHEMABRIDGE_RELEASE_REF=${SCHEMABRIDGE_RELEASE_REF}

RUN useradd --create-home --uid 1000 user
WORKDIR /home/user/app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY demo ./demo
RUN python -m pip install --no-cache-dir '.[ui,sql]'

RUN mkdir -p /tmp/schemabridge && chown -R user:user /tmp/schemabridge /home/user/app
USER user

EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:7860/_stcore/health', timeout=3).read().strip() == b'ok'"

CMD ["streamlit", "run", "src/schemabridge/entrypoints/streamlit/app.py", "--server.address=0.0.0.0", "--server.port=7860", "--server.headless=true", "--server.fileWatcherType=none", "--browser.gatherUsageStats=false"]
