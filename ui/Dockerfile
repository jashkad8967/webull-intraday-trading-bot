FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN addgroup --system dashboard \
    && adduser --system --ingroup dashboard dashboard \
    && mkdir -p /var/commands \
    && chmod 1777 /var/commands

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY --chown=dashboard:dashboard . .

USER dashboard

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
