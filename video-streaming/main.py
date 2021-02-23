from video_streaming.extensions import celery_app, grpc_server


__all__ = ['celery_app']


def start_grpc_server():
    """Start gRPC server"""
    grpc_server.serve()


def main():
    """Main entry point"""
    start_grpc_server()


if __name__ == '__main__':
    main()
