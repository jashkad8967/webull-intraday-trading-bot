FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd --create-home bot && mkdir -p /app/data && chown -R bot:bot /app
USER bot
EXPOSE 8000
CMD ["uvicorn", "app.dashboard.api:app", "--host", "0.0.0.0", "--port", "8000"]
