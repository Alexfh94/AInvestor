FROM python:3.12-slim

ENV TZ=Europe/Madrid
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY ainvestor ./ainvestor
COPY config ./config
COPY dashboard ./dashboard
COPY scripts ./scripts

RUN pip install --no-cache-dir .

RUN mkdir -p /app/data

EXPOSE 8000

CMD ["uvicorn", "ainvestor.main:app", "--host", "0.0.0.0", "--port", "8000"]
