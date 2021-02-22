#!/bin/sh

# TODO : create python command class
python -m grpc_tools.protoc -I./protos --python_out=./protos --grpc_python_out=./protos ./protos/streaming.proto

