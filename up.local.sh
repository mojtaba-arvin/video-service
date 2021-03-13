#!/bin/sh
docker-compose \
    -f docker-compose.yml \
    -f docker-compose.local.yml \
    -f docker-compose.join.redis.yml \
    -f docker-compose.join.minio.yml \
    -f docker-compose.join.rabbitmq.yml \
    up -d
