# **[Python Video Streaming Microservice](https://github.com/mojtaba-arvin/video-service)**

![video-service](https://user-images.githubusercontent.com/6056661/110987240-0d7ab780-8384-11eb-8e20-05a0a9144d35.png)

Python video streaming microservice, allows you to change the number of qualities or formats,
to reduce processing and storage space costs.

For example, instead of building both HLS and MPEG-DASH playlists, you can build a HLS with `fmp4`, that similar to `MPEG-DASH`, which reduces costs by 50%. 

The multi-stage dockerfile of the project uses `Python3.9.2` and **[FFmpeg](https://ffmpeg.org)** 4.1 that supports `fmp4` HLS segment type.

Features:
* Supports multi outputs with custom encode formats, codecs and qualities
* Supports HLS, MPEG-DASH and HLS with fmp4 segments (CMAF)
* Supports any S3-compatible object storage like Minio object storage
* Supports **[gRPC](https://grpc.io/)** protocol for low latency and high throughput communication
* No use of web frameworks to avoid unnecessary abstractions and dependencies
* Supports tracking of outputs status and returns progress details of each input or output, including **checking, processing and uploading progress**
* Supports force stop a list of jobs, to kill processes and delete all local files of every job. or revoke one output of a job
* All tasks are separate to manage and retry each part again in some exceptions
* Supports webhook callback when all outputs uploaded
* Returns input video file details to client (using FFprobe), before video processing starting. ( client can use this information to show to the end user)
* Returns **CPU and memory usage** with spent time of every output for the financial purposes when a video is being processed to create a playlist 
* Supports to generate different thumbnails from the input video by list of times, to choose one of them by user as a player poster or other purpose such as screenshots for demo 

TODO
* adding watermark
* adding optional argument to mapping playlist to different qualities as separated videos
* call webhook url when every output was done
* adding gRPC client sample and test cases
* update document

## Setup

### 1. Requirements

 * a **[Redis](https://redis.io/)** service ( to cache result and as a backend for celery )
 * a message broker ( e.g. **[RabbitMQ](https://www.rabbitmq.com/)** or Redis )
 * an S3-compatible object storage ( e.g. **[Minio](https://min.io/)** )


if you have not already, a Redis, a broker and an S3 object storage,
you can `clone` these repositories:

* Redis: **[Simple Redis service](https://github.com/mojtaba-arvin/redis)**
  ```
  git clone https://github.com/mojtaba-arvin/redis.git
  ```
* RabbitMQ: **[Simple RabbitMQ service](https://github.com/mojtaba-arvin/rabbitmq)**
  ```
  git clone https://github.com/mojtaba-arvin/rabbitmq.git
  ```
* Minio: **[Simple S3-compatible object storage service using Minio](https://github.com/mojtaba-arvin/minio)**
  ```
  git clone https://github.com/mojtaba-arvin/minio.git
  ```


#### up workflow

![build-workflow](https://user-images.githubusercontent.com/6056661/111037785-f64ad100-843a-11eb-9450-3197e05fc696.png)


### 2. Setup video service project environments

You should create an `.env` file at `video-streaming/video_streaming`
. There is a sample of required environments that you can use it:
```
cp video-streaming/video_streaming/.env.local video-streaming/video_streaming/.env
```
The project uses `python-decouple` package, you can add other variables and cast them in `settings.py`. or anywhere in project using `RepositoryEnv` class

|    | VARIABLE                         | DESCRIPTION                                                                 |
|----|----------------------------------|-----------------------------------------------------------------------------|
| 1  | CELERY_BROKER_URL                | Celery needs a message broker url, e.g. RabbitMQ url                        |
| 2  | CELERY_RESULT_BACKEND            | To keep track of Celery tasks results, e.g. Redis url                       |
| 3  | TASK_RETRY_BACKOFF_MAX           |                                                                             |
| 4  | TASK_RETRY_FFMPEG_COMMAND_MAX    |                                                                             |
| 5  | S3_ENDPOINT_URL                  |                                                                             |
| 6  | S3_ACCESS_KEY_ID                 |                                                                             |
| 7  | S3_SECRET_ACCESS_KEY             |                                                                             |
| 8  | S3_REGION_NAME                   |                                                                             |
| 9  | S3_IS_SECURE                     | Default is False but note that not all services support non-ssl connections.|
| 10 | S3_DEFAULT_BUCKET                | Default bucket name                                                         |
| 11 | S3_DEFAULT_INPUT_BUCKET_NAME     | Default bucket name of S3 storage to download videos                        |
| 12 | S3_DEFAULT_OUTPUT_BUCKET_NAME    | Default bucket name of S3 storage to upload videos                          |
| 13 | S3_TRANSFER_MULTIPART_THRESHOLD  |                                                                             |
| 14 | S3_TRANSFER_MAX_CONCURRENCY      |                                                                             |
| 15 | S3_TRANSFER_MULTIPART_CHUNKSIZE  |                                                                             |
| 16 | S3_TRANSFER_NUM_DOWNLOAD_ATTEMPTS|                                                                             |
| 17 | S3_TRANSFER_MAX_IO_QUEUE         |                                                                             |
| 18 | S3_TRANSFER_IO_CHUNKSIZE         |                                                                             |
| 19 | S3_TRANSFER_USE_THREADS          |                                                                             |
| 20 | TMP_DOWNLOADED_DIR               | Directory for temporary downloaded videos (should be volume on compose file)|                                                                             |
| 21 | TMP_TRANSCODED_DIR               | Directory for temporary transcoded videos (should be volume on compose file)|
| 22 | FFPROBE_BIN_PATH                 |                                                                             |
| 23 | FFMPEG_BIN_PATH                  |                                                                             |
| 24 | REDIS_TIMEOUT_SECOND             |                                                                             |
| 25 | REDIS_URL                        | Redis url                                                                   |


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
* `CELERY_APP`: The name of celery instance in the main module.
* `BIN_PATH`: Python installed at `/usr/local/bin/` in the Python Docker Official Image.
* `GRPC_PORT`: The gRPC port, if you change it, make sure it's exposed on your network.
  
after any change in `.circus.ini` you need to build image again.

### 5. Docker composes

in the first time, before up using `docker-compose.join.*` files, create external networks by build **[Redis](https://github.com/mojtaba-arvin/redis)**, **[RabbitMQ](https://github.com/mojtaba-arvin/rabbitmq)** and **[Minio](https://github.com/mojtaba-arvin/minio)** projects.

|    | Compose file                     | Description                                                                 |
|----|----------------------------------|-----------------------------------------------------------------------------|
| 1  | docker-compose.yml               | includes `video-streaming` service that has `WAIT_FOR` environment variable |
| 2  | docker-compose.local.yml         | for local development, maps exposed gRPC port to `8081` as your host port   |
| 3  | docker-compose.join.redis.yml    | to join the Redis service network as an external network. see : **[Redis service](https://github.com/mojtaba-arvin/redis)**|
| 4  | docker-compose.join.rabbitmq.yml | to join the RabbitMQ service network as an external network. see : **[RabbitMQ service](https://github.com/mojtaba-arvin/rabbitmq)**|
| 5  | docker-compose.join.minio.yml    | to join the Minio service network as an external network. see : **[Minio service](https://github.com/mojtaba-arvin/minio)**|

there are some `sh` scripts in the root directory of this repository, that you can use them:

* Services are built once
  ```
  bash ./build.sh
  ```
* Builds, (re)creates, starts, and attaches to containers for a service.
  ```
  bash ./up.sh
  ```
* like `up.sh` but for local development, if you are not using a reverse proxy, you can use `up.local.sh`
  that maps exposed grpc port to `8081` as your host port
  ```
  bash ./up.local.sh
  ```
* Shows services states
  ```
  bash ./ps.sh
  ```
* Displays log output from services.
  ```
  bash ./logs.sh
  ```
* To get an interactive prompt in `video-streaming` service
  ```
  bash ./exec.sh
  ```
* Stops containers and removes containers, networks, volumes, and images created by `up`.
  ```
  bash ./down.sh
  ```

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

