#!/bin/sh
docker-compose \
     -f docker-compose.video-streaming.yml \
     exec video-streaming /bin/sh -c "sh video-streaming/grpc/generate_grpc_codes.sh"
