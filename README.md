# video-service
Dockerized video processing service, using Circus to run gRPC server and Celery task queue. 


### config circus

This project uses `circusd`, to manage processes, 

`.circus.ini` file is git ignored, you need have a `.circus.ini` at `.docker-compose/video-streaming/circus/` directory. 

for local development you can use the sample by following command:

`cp .docker-compose/video-streaming/circus/circus.local.ini .docker-compose/video-streaming/circus/circus.ini`

there are some variables in the `[env]` section:

* `WORKING_DIR`: The path on the container that gRPC server.py is located
* `BIN_PATH`: Python installed at `/usr/local/bin/` in the Python Docker Official Image
* `GRPC_PORT`: The gRPC port, if you change it, make sure it's exposed on your network.

after any change in `.circus.ini` you need to build image again.

### Generate gRPC modules

generated grpc modules are added to `.gitignore`, to generate them again, you can use `generate_grpc_codes.sh` inside the container. by following command:

`docker-compose -f docker-compose.video-streaming.yml exec video-streaming /bin/sh -c "sh video-streaming/grpc/generate_grpc_codes.sh"`

this script also will change import statement to fix `ModuleNotFoundError`. 

* after any changes on gRPC proto file, you need run the script.
