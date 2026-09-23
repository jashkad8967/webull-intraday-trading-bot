# Multi-stage: the build stage carries pip, wheels and compilers; the
# runtime stage gets only the installed packages. Previously this was
# a single stage, so pip's own install machinery and every transient
# build artifact shipped to production.
#
# This host is a 2-core/1GB instance with an 8.7G root that filled to
# 99% on 2026-09-23 and wedged Docker, taking the bot down mid-session
# with open positions. Image size is not cosmetic here.
FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt \
    && find /opt/venv -name '__pycache__' -type d -prune -exec rm -rf {} + \
    && find /opt/venv -name '*.pyc' -delete


FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

RUN addgroup --system trader \
    && adduser --system --ingroup trader trader \
    && mkdir -p /var/data/logs /var/data/conf /var/commands \
    && chown -R trader:trader /app /var/data \
    && chmod 1777 /var/commands

COPY --from=build /opt/venv /opt/venv

COPY --chown=trader:trader src/ src/
# The dashboard now lives in this image too - see deploy/entrypoint.sh
# for why the two containers were collapsed into one.
COPY --chown=trader:trader ui/server.py ui/
COPY --chown=trader:trader ui/static/ ui/static/
COPY --chown=trader:trader deploy/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod 0755 /usr/local/bin/entrypoint.sh

USER trader

CMD ["/usr/local/bin/entrypoint.sh"]
