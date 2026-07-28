FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

WORKDIR /app

RUN addgroup --system trader \
    && adduser --system --ingroup trader trader \
    && mkdir -p /var/data/logs /var/data/conf /var/commands \
    && chown -R trader:trader /app /var/data \
    && chmod 1777 /var/commands

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY --chown=trader:trader src/ src/

USER trader

CMD ["python", "-m", "webull_bot"]
