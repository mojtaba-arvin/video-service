#!/bin/sh
docker-compose \
     -f docker-compose.video-streaming.local.yml \
     exec video-streaming /bin/sh
