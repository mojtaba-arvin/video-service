#!/bin/sh
docker-compose \
     -f docker-compose.video-streaming.yml \
     -f docker-compose.expose.yml \
     -f docker-compose.messaging.local.yml \
     up -d
