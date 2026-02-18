FROM python:3.11-slim

# Prevent Python buffering logs
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# System deps (minimal)
RUN apt-get update && apt-get install -y \
    curl unzip ca-certificates \
    && curl -fsSL https://releases.hashicorp.com/nomad/1.11.1/nomad_1.11.1_linux_amd64.zip -o nomad.zip \
    && unzip nomad.zip \
    && mv nomad /usr/local/bin/ \
    && rm nomad.zip \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY app.py .
COPY templates ./templates


CMD ["python", "app.py"]