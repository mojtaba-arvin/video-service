#!/bin/sh
docker-compose \
     -f docker-compose.video-streaming.yml \
     -f docker-compose.expose.yml \
     up -d
