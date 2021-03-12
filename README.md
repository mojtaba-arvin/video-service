# Python Video Streaming Microservice

![video-service](https://user-images.githubusercontent.com/6056661/110987240-0d7ab780-8384-11eb-8e20-05a0a9144d35.png)

Python video streaming microservice, allows you to change the number of qualities or formats,
to reduce processing and storage space costs.

For example, instead of building both HLS and MPEG-DASH playlists, you can build a HLS with `fmp4`, that similar to `MPEG-DASH`, which reduces costs by 50%. 

The multi-stage dockerfile of project uses `Python3.9.2` and **[FFmpeg](https://ffmpeg.org)** 4.1 that support `fmp4` hls segment type.


Features:
* Supports multi outputs with custom encode formats, codecs and qualities
* Supports HLS, MPEG-DASH and HLS with fmp4 segments (CMAF)
* Supports any S3-compatible object storage like Minio object storage
* Supports **[gRPC](https://grpc.io/)** protocol for low latency and high throughput communication
* No use of web frameworks to avoid unnecessary abstractions and dependencies
* Supports tracking of outputs status and returns progress details of each input or output, including total **passed checking, processing and uploading progress**
* Supports force stop outputs, to kill processes and delete all local files
* All tasks are separate to manage and retry each part again in some exceptions
* Supports webhook callback when all outputs uploaded

TODO
* return input video file details to client, before video processing starting. ( client can use this information to show the end user)
* revoke one output of a job
* adding watermark
* call webhook url when every output was done
* adding gRPC client sample and test cases
* update document

### 1. Setup Redis and RabbitMQ
for local developments you can use Redis and Rabbit services in this repository,
but you should configure them before building.

#### Redis Service

to set `Redis` password ,just put password value at
`.docker-compose/redis/` directory as `.redis_pass_file` file, or use the sample:
```
cp .docker-compose/redis/.redis_pass_file.local .docker-compose/redis/.redis_pass_file
```
#### RabbitMQ Service

`RabbitMQ` environment ,must be at `.docker-compose/rabbitmq/` directory as `.env` file. you can copy the sample:
```
cp .docker-compose/rabbitmq/.env.local .docker-compose/rabbitmq/.env
```
### 2. Project environment

This project needs a message broker and result backend for Celery.
You should create an `.env` file at `video-streaming/video_streaming`
. There is a sample of required environments that you can use it:
```
cp video-streaming/video_streaming/.env.local video-streaming/video_streaming/.env
```
The project uses `python-decouple` package, you can add other variables and cast them in `settings.py`. or anywhere in project using `RepositoryEnv` class

|    | VARIABLE                     | DESCRIPTION                                                                 |
|----|------------------------------|-----------------------------------------------------------------------------|
| 1  | CELERY_BROKER_URL            | Celery needs a message broker url, e.g. RabbitMQ url                        |
| 2  | CELERY_RESULT_BACKEND        | To keep track of Celery tasks results, e.g. Redis url                       |
| 3  | S3_ENDPOINT_URL              |                                                                             |
| 4  | S3_ACCESS_KEY_ID             |                                                                             |
| 5  | S3_SECRET_ACCESS_KEY         |                                                                             |
| 6  | S3_REGION_NAME               |                                                                             |
| 7  | S3_IS_SECURE                 | Default is False but note that not all services support non-ssl connections.|
| 8  | S3_DEFAULT_BUCKET            | Default bucket name                                                         |
| 9  | S3_DEFAULT_INPUT_BUCKET_NAME | Default bucket name of S3 storage to download videos                        |
| 10 | S3_DEFAULT_OUTPUT_BUCKET_NAME| Default bucket name of S3 storage to upload videos                          |


### 3. Generate Certificates to use by gRPC
TODO

### 4. Config circus

This project uses `circusd`, to manage processes, 

`.circus.ini` file is git ignored, you need have a `.circus.ini` at `.docker-compose/video-streaming/circus/` directory. 

for local development you can use the sample by following command:
```
cp .docker-compose/video-streaming/circus/circus.local.ini .docker-compose/video-streaming/circus/circus.ini
```
there are some variables in the `[env]` section:

* `WORKING_DIR`: The path on the container that main module is located.
* `MODULE_NAME`: The name of main module of project.
* `CELERY_APP` : The name of celery instance in the main module.
* `BIN_PATH`: Python installed at `/usr/local/bin/` in the Python Docker Official Image.
* `GRPC_PORT`: The gRPC port, if you change it, make sure it's exposed on your network.
  
after any change in `.circus.ini` you need to build image again.

### 5. Building Docker composes

##### Building for local development

there are some `sh` scripts in the root directory, that you can use them:

* `bash ./build.sh` : Services are built once

* `bash ./up.sh` : Builds, (re)creates, starts, and attaches to containers for a service.

* `bash ./ps.sh` : Shows services states

* `bash ./logs.sh` : Displays log output from services.

* `bash ./exec.video-streaming.sh` : To get an interactive prompt in `video-streaming` service

* `bash ./down.sh` : Stops containers and removes containers, networks, volumes, and images created by `up`

##### Building for production
TODO

### 6. Generate gRPC modules

generated grpc modules are added to `.gitignore`, to generate them again, you can use the following command:
```
bash ./exec.codegen.sh
```
it runs `generate_grpc_codes.sh` inside `video-streaming` container that also will change import statement to fix `ModuleNotFoundError`. 

* after any changes on the gRPC proto file, you need run the script again.

### APPs

apps located at `video-streaming/video_streaming/`


|    | APP_NAME    | DESCRIPTION                         |
|----|-------------|-------------------------------------|
| 1  | core        | base classes and common modules     |
| 2  | grpc        | the inclusion root of gRPC          |
| 3  | ffmpeg      | video processing tasks using ffmpeg |

after create a new app, to discover celery tasks, add the app to `AUTO_DISCOVER_TASKS` in `settings.py`.

