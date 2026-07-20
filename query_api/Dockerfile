FROM python:3.13-slim-bookworm@sha256:9d7f287598e1a5a978c015ee176d8216435aaf335ed69ac3c38dd1bbb10e8d64 AS dependencies
WORKDIR /build
COPY requirements.lock .
RUN python -m pip install --no-cache-dir --require-hashes --prefix=/install -r requirements.lock

FROM python:3.13-slim-bookworm@sha256:9d7f287598e1a5a978c015ee176d8216435aaf335ed69ac3c38dd1bbb10e8d64
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/app:/app
WORKDIR /app
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app
RUN python -m pip uninstall --yes pip setuptools wheel
COPY --from=dependencies /install /usr/local
COPY --chown=app:app ./app ./app
COPY --chown=app:app alembic.ini ./alembic.ini
COPY --chown=app:app ./alembic ./alembic
USER 10001
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
