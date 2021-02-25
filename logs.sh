#!/bin/sh
docker-compose \
     -f docker-compose.video-streaming.local.yml \
     logs --follow --tail=50
