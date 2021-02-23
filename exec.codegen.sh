#!/bin/sh
docker-compose \
     -f docker-compose.video-streaming.yml \
     -f docker-compose.expose.yml \
     -f docker-compose.messaging.local.yml \
     exec video-streaming /bin/sh -c "sh video-streaming/grpc/generate_grpc_codes.sh"
