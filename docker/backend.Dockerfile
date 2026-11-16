FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app/backend
COPY backend/ /app/backend/
RUN pip install '.[test]' && groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app \
    && mkdir -p /data/uploads /data/telemetry && chown -R app:app /data
COPY --chmod=755 scripts/container-entrypoint.sh /usr/local/bin/ticket-entrypoint
COPY scripts/prepare_database.py /usr/local/bin/ticket-prepare-db
USER app
ENTRYPOINT ["ticket-entrypoint"]
CMD ["api"]
