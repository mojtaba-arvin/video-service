from grpc_health.v1 import health, health_pb2_grpc


class Health(health.HealthServicer):

    def _add_to_server(self, server):
        health_pb2_grpc.add_HealthServicer_to_server(
            self,
            server)
        return self
