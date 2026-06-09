FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY backend ./backend
COPY frontend ./frontend
RUN python -m pip install --no-cache-dir .
RUN python -m pip install --no-cache-dir tinyshare \
    -i https://minidoc.pages.dev/simple/

RUN mkdir -p /app/data
EXPOSE 8000

CMD ["uvicorn", "quantdev.api:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000"]
