from grpc_health.v1 import health, health_pb2_grpc


class Health(health.HealthServicer):

    def _add_to_server(self, server):
        health_servicer = self.__class__()
        health_pb2_grpc.add_HealthServicer_to_server(
            health_servicer,
            server)
        return health_servicer
