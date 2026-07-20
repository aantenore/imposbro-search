FROM python:3.14-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6 AS dependencies
WORKDIR /build
COPY requirements.lock .
RUN python -m pip install --no-cache-dir --require-hashes --prefix=/install -r requirements.lock

FROM python:3.14-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app
RUN python -m pip uninstall --yes pip setuptools wheel
COPY --from=dependencies /install /usr/local
COPY --chown=app:app ./app .
USER 10001
CMD ["python", "-u", "main.py"]
