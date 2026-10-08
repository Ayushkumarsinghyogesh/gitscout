# gitscout as a long-running scheduler.
#
#   docker build -t gitscout .
#   docker run --rm --env-file .env -v gitscout-data:/data gitscout
#
# The default command watches the shipped cloud-security target profile every 6
# hours and writes each run's *new* leads to /data. Override it to do anything else:
#
#   docker run --rm --env-file .env -v gitscout-data:/data gitscout \
#     gitscout scout prowler-cloud/prowler -o /data/leads.csv
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    GITSCOUT_DB=/data/gitscout.db

WORKDIR /app

# Dependencies first, so code changes do not invalidate the layer.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && mkdir -p /data

# Run as a non-root user; /data must stay writable for the SQLite WAL files.
RUN useradd --create-home --uid 10001 scout && chown -R scout:scout /data
USER scout

VOLUME ["/data"]

# A container with no reachable GitHub API or a bad token should fail fast and loudly.
HEALTHCHECK --interval=10m --timeout=60s --start-period=30s --retries=3 \
    CMD gitscout doctor --offline || exit 1

ENTRYPOINT []
CMD ["gitscout", "watch", \
     "--targets", "cloud-security", \
     "--every", "6h", \
     "--out", "/data/new-leads.csv", \
     "--out", "/data/new-leads.jsonl", \
     "--only-with-email"]
