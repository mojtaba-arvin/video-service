#!/bin/sh
docker-compose \
    -f docker-compose.yml \
     logs --follow --tail=50
