# video-service
Dockerized video processing service, using Circus to run gRPC server and Celery task queue. 


### config circus

This project uses `circusd`, to manage processes, 

`.circus.ini` file is git ignored, you need have a `.circus.ini` at `.docker-compose/video-streaming/circus/` directory. 

for local development you can use the sample:

`cp .docker-compose/video-streaming/circus/circus.local.ini .docker-compose/video-streaming/circus/circus.ini`

there are some variables in the `[env]` section:

* `WORKING_DIR`: The path on the container that gRPC server.py is located
* `BIN_PATH`: python installed at `/usr/local/bin/` in the official Python Docker image
* `GRPC_PORT`: The gRPC port, if you change it, make sure it's exposed on your network.

after any change in `.circus.ini` you need to build image again.
