# Simulation host for fledge-south-restclient.
#
# This image does NOT contain the Fledge plugin - it is the fake REST endpoint
# the plugin is pointed at: `simulator/serve_fixtures.py` plus the process model in
# `simulator/process_simulator.py`, serving every fixture under
# `tests/fixtures/payloads/` as live-looking data on a 15-second tick.
#
#   cd simulator && docker compose up -d --build
#   curl -s http://127.0.0.1:4444/site_snapshot.json | jq .
#
# The build context is the REPO ROOT, not this directory - the image needs the
# fixture payloads too. Building by hand:
#
#   docker build -f simulator/Dockerfile -t restclient-sim .
#
# See docs/rest-testing.rst.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Pick up Debian security updates published after the base image was built.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

COPY simulator/requirements-sim.txt ./simulator/requirements-sim.txt
RUN pip install --no-cache-dir -r simulator/requirements-sim.txt

# The host resolves the payload directory relative to its own location, so the
# repo layout has to be preserved inside the image.
COPY simulator/ ./simulator/
COPY tests/fixtures/payloads/ ./tests/fixtures/payloads/

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin sim
USER sim

EXPOSE 4444

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4444/', timeout=3).read()"]

# --workers 1 is required: the process model is in-process state, so a second
# worker would be a second, divergent model and pollers would flip between them.
# Concurrency comes from threads instead.
CMD ["gunicorn", \
     "--chdir", "/app/simulator", \
     "--bind", "0.0.0.0:4444", \
     "--workers", "1", \
     "--threads", "4", \
     "--access-logfile", "-", \
     "serve_fixtures:app"]
