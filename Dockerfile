FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN grep -v -E -i 'cuda|nvidia|^torch([<>=!~].*)?$|^torchvision([<>=!~].*)?$' requirements.txt > req-clean.txt && \
    pip install --no-cache-dir \
        torch==2.13.0+cpu \
        torchvision==0.28.0+cpu \
        --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r req-clean.txt

COPY . /app
RUN mkdir -p /data

ENV PYTHONUNBUFFERED=1
EXPOSE 5000
ENTRYPOINT ["/app/docker-entrypoint.sh"]