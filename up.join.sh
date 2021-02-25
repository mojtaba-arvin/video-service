#!/bin/sh
docker-compose \
     -f docker-compose.video-streaming.local.yml \
     -f docker-compose.video-streaming.join.minio.yml \
     up -d
