#!/bin/sh
docker-compose \
    -f docker-compose.yml \
     exec video-streaming /bin/sh
