#!/bin/sh
docker-compose \
     -f docker-compose.video-streaming.local.yml \
     -f docker-compose.expose.yml \
     ps
