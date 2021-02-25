#!/bin/sh
docker-compose \
     -f docker-compose.video-streaming.local.yml \
     exec video-streaming /bin/sh -c "cd video-streaming/video_streaming/grpc/ && sh ./generate_grpc_codes.sh"
