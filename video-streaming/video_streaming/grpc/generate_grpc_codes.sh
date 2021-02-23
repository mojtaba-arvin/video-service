#!/bin/sh

# TODO : create python command class
python -m grpc_tools.protoc -I./protos --python_out=./protos --grpc_python_out=./protos ./protos/streaming.proto

# to fix ModuleNotFoundError
sed -i -e "s/import streaming_pb2/from . import streaming_pb2/g" ./protos/streaming_pb2_grpc.py

# TODO : restart gRPC server
