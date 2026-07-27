FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN addgroup --system trader \
    && adduser --system --ingroup trader trader \
    && mkdir -p /var/data/logs /var/data/conf \
    && chown -R trader:trader /app /var/data

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY --chown=trader:trader . .

USER trader

CMD ["python", "bot.py"]
