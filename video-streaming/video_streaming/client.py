import argparse
import time

import grpc

from video_streaming.grpc.protos import streaming_pb2, streaming_pb2_grpc


def command_arguments():
    parser = argparse.ArgumentParser(description='GRPC-based streaming client.')
    parser.add_argument(
        '--host',
        type=str,
        required=True,
        help='The server hostname or address.'
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
        help='CA cert or bundle.'
    )
    parser.add_argument(
        '--client_cert',
        type=str,
        required=False,
        help='Client certificate used for client identification and auth.'
    )
    parser.add_argument(
        '--client_key',
        type=str,
        required=False,
        help='Client certificate key.'
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


def main():
    args = command_arguments()
    stub = build_client_stub(args)

    start_time = time.time()
    
    webhook_url = "https://www.google.com"
  
    task_request = streaming_pb2.TaskRequest(
        webhook_url=webhook_url
    )
    task_response = stub.video_processor(task_request)
    print("Got response: '{}'".format(task_response.tracking_id))
    time_total = time.time() - start_time
    print("Total time: {}\nTotal QPS: {}".format(
        time_total, 1000 / time_total))


if __name__ == '__main__':
    main()
