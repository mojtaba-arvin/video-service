#!/bin/sh

PROTOS_DIR="../video_streaming/grpc/protos"
COPY_TO="../examples"

# go to protos directory
cd "${PROTOS_DIR}" || return

# generate _pb2* modules
python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. ./streaming.proto

cd - || return

cp "${PROTOS_DIR}/streaming_pb2_grpc.py" "${COPY_TO}/streaming_pb2_grpc.py"
cp "${PROTOS_DIR}/streaming_pb2.py" "${COPY_TO}/streaming_pb2.py"

# to fix ModuleNotFoundError
sed -i -e "s/import streaming_pb2/from . import streaming_pb2/g" "${PROTOS_DIR}/streaming_pb2_grpc.py"

# TODO : restart gRPC server
