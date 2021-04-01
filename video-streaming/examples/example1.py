"""
python example1.py --host=localhost --port=9999
"""
import argparse
import time
import grpc
import streaming_pb2
import streaming_pb2_grpc


def command_arguments():
    parser = argparse.ArgumentParser(description='example client')
    parser.add_argument(
        '--host',
        type=str,
        required=True,
        help='The server address'
    )
    parser.add_argument(
        '--port',
        type=int,
        required=True,
        help='The server port'
    )
    parser.add_argument(
        '--ca_cert',
        type=str,
        required=False,
        help='CA cert or bundle'
    )
    parser.add_argument(
        '--client_cert',
        type=str,
        required=False,
        help='Client certificate used for client identification & auth'
    )
    parser.add_argument(
        '--client_key',
        type=str,
        required=False,
        help='Client certificate key'
    )

    parser.add_argument(
        '--webhook_url',
        type=str,
        required=False,
        help='Webhook url'
    )
    parser.add_argument(
        '--reference_id',
        type=str,
        required=False,
        help='Reference id'
    )
    return parser.parse_args()


def build_client_stub(cli_args):
    cert = None
    key = None
    if cli_args.client_cert:
        cert = open(cli_args.client_cert, 'rb').read()
        key = open(cli_args.client_key, 'rb').read()

    if cli_args.ca_cert:
        ca_cert = open(cli_args.ca_cert, 'rb').read()
        credentials = grpc.ssl_channel_credentials(ca_cert, key, cert)
        channel = grpc.secure_channel(
            cli_args.host + ':' + str(cli_args.port), credentials)
    else:
        channel = grpc.insecure_channel(
            cli_args.host + ':' + str(cli_args.port))

    return streaming_pb2_grpc.StreamingStub(channel)


def call_create_job():
    args = command_arguments()
    stub = build_client_stub(args)
    start_time = time.time()

    job_options = dict(

        # webhook url
        webhook_url="https://example.com",
        # reference id
        reference_id="sdfghjklcxcvbnmjhgf",
        
        video=streaming_pb2.Video(
            # download video from s3 as a input
            s3_input=streaming_pb2.S3Input(
                key="example.mp4",
                bucket="content"
            )
        ),

        watermark=streaming_pb2.Watermark(
            # download watermark image from s3 as a input
            s3_input=streaming_pb2.S3Input(
                key="logo.png",
                bucket="content"
            ),

            # upload watermarked video to s3 as a output
            upload_to=streaming_pb2.S3Output(
                key="test12/watermarked_video.mp4",
                bucket="test-watermarked-video",
                create_bucket=True,
                dont_replace=False
            ),

            position=streaming_pb2.WatermarkPosition.FIX_TOP_LEFT
        ),

        playlists=[
            streaming_pb2.PlaylistOutput(
                protocol=streaming_pb2.Protocol.HLS,
                # upload HLS playlist to s3 a output
                upload_to=streaming_pb2.S3Output(
                    key="test10/o.m3u8",
                    bucket="test-hls",
                    create_bucket=True,
                    dont_replace=False
                ),
                options=streaming_pb2.ConvertOptions(
                    fmp4=True,
                    h264=streaming_pb2.H264(
                        audio_codec=streaming_pb2.H264.AudioCodec.AAC
                    ),
                    quality_names=[
                        streaming_pb2.QualityName.R_240P,
                        streaming_pb2.QualityName.R_480P,
                        streaming_pb2.QualityName.R_720P
                    ]
                ),
                use_watermark=True
            ),
            streaming_pb2.PlaylistOutput(
                protocol=streaming_pb2.Protocol.DASH,
                # upload Dash playlist to s3 a output
                upload_to=streaming_pb2.S3Output(
                    key="test10/o.mpd",
                    bucket="test-dash",
                    create_bucket=True,
                    dont_replace=False
                ),
                options=streaming_pb2.ConvertOptions(
                    quality_names=[
                        streaming_pb2.QualityName.R_360P,
                        streaming_pb2.QualityName.R_720P
                    ],
                    # you can set custom qualities by size and bitrate
                    custom_qualities=[
                        streaming_pb2.CustomQuality(
                            size=streaming_pb2.QualitySize(
                                width=256,
                                height=144),
                            bitrate=streaming_pb2.QualityBitrate(
                                video=95 * 1024,
                                audio=64 * 1024)
                        )
                    ]
                )
            ),
        ],

        thumbnails=[
            streaming_pb2.ThumbnailOutput(
                thumbnail_time="25.65",
                # upload thumbnail to s3 a output
                upload_to=streaming_pb2.S3Output(
                    key="test11/00-00-25-650.png",
                    bucket="test-thumbnails",
                    create_bucket=True,
                    dont_replace=False
                ),
                options=streaming_pb2.ThumbnailOptions(
                    scale_width=-1,
                    scale_height=-1
                )
            ),
            streaming_pb2.ThumbnailOutput(
                thumbnail_time="00:00:14.435",
                # upload thumbnail to s3 a output
                upload_to=streaming_pb2.S3Output(
                    key="test11/00-00-14-435.png",
                    bucket="test-thumbnails",
                    create_bucket=True,
                    dont_replace=False
                ),
                options=streaming_pb2.ThumbnailOptions(
                    scale_width=-1,
                    scale_height=-1
                ),
                # generate watermarked thumbnail
                use_watermark=True,
            ),
            streaming_pb2.ThumbnailOutput(
                thumbnail_time="0",
                # upload thumbnail to s3 a output
                upload_to=streaming_pb2.S3Output(
                    key="test11/00-00-00-000.png",
                    bucket="test-thumbnails",
                    create_bucket=True,
                    dont_replace=False
                ),
                options=streaming_pb2.ThumbnailOptions(
                    scale_width=-1,
                    scale_height=-1
                ),
                use_watermark=True
            )
        ]
    )
    job_request = streaming_pb2.JobRequest(**job_options)
    print("-" * 67)
    try:
        response = stub.create_job(job_request)
        print('Call success: %s', response.tracking_id)
        return response.tracking_id
    except grpc.RpcError as rpc_error:
        print('Call failure: %s', rpc_error)

    time_total = time.time() - start_time
    print("Total time: {}\nTotal QPS: {}".format(
        time_total, 1000 / time_total))


def call_get_results(tracking_ids: list[str]):
    args = command_arguments()
    stub = build_client_stub(args)
    start_time = time.time()
    result_request = streaming_pb2.JobsResultsRequest(
        tracking_ids=tracking_ids
    )
    result_response = stub.get_results(result_request)
    print("-" * 67)
    print("Got response: '{}'".format(result_response.results))
    time_total = time.time() - start_time
    print("Total time: {}\nTotal QPS: {}".format(
        time_total, 1000 / time_total))


if __name__ == '__main__':

    tracking_id = call_create_job()
    while True:
        call_get_results([tracking_id])
        time.sleep(500 / 1000)
