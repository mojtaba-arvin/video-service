#!/bin/sh
docker-compose \
     -f docker-compose.video-streaming.yml \
     exec video-streaming /bin/sh
