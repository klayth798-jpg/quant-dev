FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/home/quantdev/.local/bin:$PATH

WORKDIR /app
COPY pyproject.toml README.md ./
COPY backend ./backend
COPY frontend ./frontend
RUN python -m pip install --no-cache-dir .

ARG INSTALL_TINYSHARE=true
RUN if [ "$INSTALL_TINYSHARE" = "true" ]; then \
      python -m pip install --no-cache-dir tinyshare \
        -i https://minidoc.pages.dev/simple/; \
    fi

RUN useradd --create-home --uid 10001 quantdev \
    && chown -R quantdev:quantdev /app
USER quantdev

EXPOSE 8000

CMD ["uvicorn", "quantdev.api:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000"]
