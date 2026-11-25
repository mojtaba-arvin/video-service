# Keep the Python runtime and FFmpeg userspace on the same Alpine/musl base.
# The previous FFmpeg 4.1 stage was built against an older Alpine release and
# copying its /usr/local tree into a newer Python image was not ABI-safe.
FROM python:3.12.11-alpine3.22

LABEL maintainer="Mojtaba Arvin m.arvin@live.com"

COPY cgroup_exec.c /opt/cgroup_exec.c

RUN python3 -m pip install --no-cache-dir --upgrade pip==26.0.1 \
    && apk add --no-cache \
        build-base \
        linux-headers \
        libffi-dev \
        libxml2-dev \
        libxslt-dev \
        postgresql-dev \
        zlib-dev \
        jpeg-dev \
        imagemagick \
        ffmpeg \
    && cc -O2 -Wall -Wextra -Werror /opt/cgroup_exec.c \
        -o /usr/local/bin/video-cgroup-exec \
    && chmod 0755 /usr/local/bin/video-cgroup-exec \
    && rm -f /opt/cgroup_exec.c

WORKDIR /opt/workdir

ENV PYTHONUNBUFFERED=1 \
    FFMPEG_BIN_PATH=/usr/bin/ffmpeg \
    FFPROBE_BIN_PATH=/usr/bin/ffprobe

COPY requirements.txt /opt/workdir/requirements.txt
RUN pip3 install --no-cache-dir --default-timeout=100 \
    --requirement /opt/workdir/requirements.txt

COPY circus/circus.ini /etc/circus.ini

COPY ./sh/*.sh /docker/bin/
RUN chmod a+x /docker/bin/*.sh && sed -i -e 's/\r$//' /docker/bin/*.sh

ENTRYPOINT ["/docker/bin/run.sh"]
