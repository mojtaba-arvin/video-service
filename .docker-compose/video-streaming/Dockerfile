FROM jrottenberg/ffmpeg:4.1-alpine as ffmpeg
LABEL maintainer="Mojtaba Arvin m.arvin@live.com"

# Not yet published grpcio Wheel for python3.10
# FROM python:3.10.0a5-alpine

FROM python:3.9.2-alpine
RUN apk add --update --no-cache alpine-sdk linux-headers musl-dev libffi-dev py3-libxml2 libxslt-dev zlib-dev jpeg-dev imagemagick
COPY --from=ffmpeg /usr/local /usr/local

# set workdir
RUN mkdir -p /opt/workdir
WORKDIR /opt/workdir

ENV PYTHONUNBUFFERED 1

ADD requirements.txt /opt/workdir
RUN pip3 install --no-cache-dir --default-timeout=100 -r /opt/workdir/requirements.txt

ADD circus/circus.ini /etc/circus.ini


COPY ["./sh/*.sh", "/docker/bin/"]
RUN chmod a+x /docker/bin/*.sh && sed -i -e 's/\r$//' /docker/bin/*.sh

# wait for dependencies and run the circusd
ENTRYPOINT ["/docker/bin/run.sh"]
