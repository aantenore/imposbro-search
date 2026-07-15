FROM python:3.11-slim@sha256:e031123e3d85762b141ad1cbc56452ba69c6e722ebf2f042cc0dc86c47c0d8b3 AS dependencies
WORKDIR /build
COPY requirements.lock .
RUN python -m pip install --no-cache-dir --require-hashes --prefix=/install -r requirements.lock

FROM python:3.11-slim@sha256:e031123e3d85762b141ad1cbc56452ba69c6e722ebf2f042cc0dc86c47c0d8b3
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app
RUN python -m pip uninstall --yes pip setuptools wheel
COPY --from=dependencies /install /usr/local
COPY --chown=app:app ./app .
USER 10001
CMD ["python", "-u", "main.py"]
