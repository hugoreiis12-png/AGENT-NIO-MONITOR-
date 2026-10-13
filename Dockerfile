FROM python:3.12-slim

# Roda a partir do codigo-fonte em /app: o loader acha config/ e migrations/ pela
# posicao do pacote (parents[3]), entao o pacote NAO pode ficar so em site-packages.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install -e .

COPY config ./config
COPY migrations ./migrations

RUN useradd --system --no-create-home --uid 10001 monitor
USER monitor

EXPOSE 8099

CMD ["python", "-m", "agent_nio_monitor", "run"]
