# video-service
Dockerized video processing service, using Circus to run gRPC server and Celery task queue. 

The `video-streaming` multi-stage dockerfile uses `Python3.9.2` and `ffmpeg4.1` that support `fmp4` hls segment type that similar to `MPEG-DASH` and have some advantages over `MPEG-TS`.

For video processing, `python-ffmpeg-video-streaming` package has been installed and you can get or put your files to a cloud such as `Amazon S3` compatible storages, `Google Cloud Storage` and `Microsoft Azure Storage` 

### Config circus

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

generated grpc modules are added to `.gitignore`, to generate them again, you can use the following command:

`bash ./exec.codegen.sh`

it runs `generate_grpc_codes.sh` inside `video-streaming` container that also will change import statement to fix `ModuleNotFoundError`. 

* after any changes on the gRPC proto file, you need run the script again.

### docker-compose

there are some `sh` scripts in the root directory, that you can use them:


* `bash ./build.sh` : Services are built once

* `bash ./up.sh` : Builds, (re)creates, starts, and attaches to containers for a service.

* `bash ./ps.sh` : Shows services states

* `bash ./logs.sh` : Displays log output from services.

* `bash ./exec.video-streaming.sh` : To get an interactive prompt in `video-streaming` service

* `bash ./down.sh` : Stops containers and removes containers, networks, volumes, and images created by `up`

