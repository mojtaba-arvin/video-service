#!/bin/sh
docker-compose \
     -f docker-compose.video-streaming.yml \
     logs --follow --tail=50
