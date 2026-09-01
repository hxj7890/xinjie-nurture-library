FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
# Production release archives carry a wheelhouse built by GitHub Actions. This
# keeps deployment independent of the production host's public-PyPI speed.
# The mirror path is retained only for local/CI image builds without wheels.
COPY vendor/wheels /wheels
RUN if find /wheels -type f -name '*.whl' -print -quit | grep -q .; then \
      pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt; \
    else \
      pip install --no-cache-dir --timeout 45 --retries 2 \
        -i https://pypi.tuna.tsinghua.edu.cn/simple \
        --extra-index-url https://pypi.org/simple \
        -r requirements.txt; \
    fi
COPY . .
ENV APP_DB_PATH=/data/nurture_library.sqlite3 APP_MEDIA_DIR=/media PORT=8040
EXPOSE 8040
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
