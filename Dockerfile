FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV APP_DB_PATH=/data/nurture_library.sqlite3 APP_MEDIA_DIR=/media PORT=8040
EXPOSE 8040
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]

