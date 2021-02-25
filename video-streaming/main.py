from video_streaming.grpc import GrpcServer
from video_streaming.celery import celery_app

# To found main.celery_app module and load celery application.
__all__ = ['celery_app']


def main():
    """Main entry point"""

    GrpcServer().serve()  # start gRPC server


if __name__ == '__main__':
    main()
