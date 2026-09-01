FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
# The production host is in China.  Use a nearby PyPI mirror first, with the
# public index as a fallback, so a cold image build cannot stall deployment.
RUN pip install --no-cache-dir --timeout 45 --retries 2 \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    --extra-index-url https://pypi.org/simple \
    -r requirements.txt
COPY . .
ENV APP_DB_PATH=/data/nurture_library.sqlite3 APP_MEDIA_DIR=/media PORT=8040
EXPOSE 8040
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
