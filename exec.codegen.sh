#!/bin/sh
docker-compose \
    -f docker-compose.yml \
     exec video-streaming /bin/sh -c "cd video-streaming/video_streaming/grpc/ && sh ./generate_grpc_codes.sh"
