# syntax=docker/dockerfile:1

FROM dhi.io/python:3.12-dev AS builder

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH=/opt/venv/bin:$PATH

WORKDIR /app

RUN python -m venv /opt/venv
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY static ./static
RUN mkdir -p /app/data/builds
ARG BUILD_VERSION=dev
RUN echo "$BUILD_VERSION" > /app/BUILD_VERSION

FROM dhi.io/python:3.12

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV REVIEW_BIND=0.0.0.0
ENV PORT=8080
ENV REVIEW_DATA_DIR=/app/data
ENV PATH=/opt/venv/bin:$PATH

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder --chown=nonroot:nonroot /app /app

EXPOSE 8080

CMD ["python", "src/main.py"]
