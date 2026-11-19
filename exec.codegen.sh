#!/bin/sh
docker-compose \
    -f docker-compose.yml \
     exec video-streaming /bin/sh -c "cd video-streaming/scripts/ && sh ./generate_grpc_codes.sh"
