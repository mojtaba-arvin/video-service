FROM jrottenberg/ffmpeg:4.1-ubuntu
LABEL maintainer="Mojtaba Arvin m.arvin@live.com"

# install python3.10
RUN apt-get update && apt-get install software-properties-common -y && add-apt-repository ppa:deadsnakes/ppa && \
    apt-get update && apt-get install python3.10 python3-pip -y

# create symlinks
RUN ln -s /usr/bin/python3.10 /usr/bin/python & \
    ln -s /usr/bin/pip3 /usr/bin/pip

# set workdir
RUN mkdir -p /opt/workdir
WORKDIR /opt/workdir

ENV PYTHONUNBUFFERED 1

ADD requirements.txt /opt/workdir
RUN pip3 install --no-cache-dir --default-timeout=100 -r /opt/workdir/requirements.txt

# TODO use circusd

ENTRYPOINT ["tail", "-f", "/dev/null"]
