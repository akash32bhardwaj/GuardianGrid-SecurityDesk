FROM python:3.12-slim

# Site-local time. Every "today" boundary in the app — daily counts, the
# morning brief, reports, the ack windows — is built on datetime.now(), which
# reads the container's clock. With no timezone set that clock is UTC, so a
# day rolled over at 05:30 IST and the whole overnight window, the part that
# matters most on a security site, landed in the wrong day.
# Override per site with -e TZ=... if a client is ever in another zone.
ENV TZ=Asia/Kolkata

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libgl1 libglib2.0-0 tzdata \
    fonts-noto-core \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
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

# The commit this image was built from. deploy.sh passes it and then
# reads it back out of the running container, so "deployed" is a fact
# rather than a claim. Three times in one session a fix looked broken
# when it simply had not reached the server.
ARG GIT_SHA=unknown
ENV OCTA_GIT_SHA=$GIT_SHA

EXPOSE 5000
ENTRYPOINT ["/app/docker-entrypoint.sh"]