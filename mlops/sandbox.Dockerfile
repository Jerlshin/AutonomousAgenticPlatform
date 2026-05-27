FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN useradd --create-home --shell /usr/sbin/nologin agent

WORKDIR /workspace
USER agent

CMD ["python", "--version"]
