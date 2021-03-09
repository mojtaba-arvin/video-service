from video_streaming.cache import RedisCache
from video_streaming.core.constants import CacheKeysTemplates
from video_streaming.grpc.protos import streaming_pb2


class GetResultsMixin(object):

    cache = RedisCache()
    pb2 = streaming_pb2

    def _outputs(self, request_id, total_outputs):
        outputs: list[streaming_pb2.OutputProgress] = []
        for output_number in range(total_outputs):
            output_status = self.cache.get(
                CacheKeysTemplates.OUTPUT_STATUS.format(
                    request_id=request_id,
                    output_number=output_number),
                decode=False)
            if output_status:
                progress: dict = self.cache.get(
                    CacheKeysTemplates.OUTPUT_PROGRESS.format(
                        request_id=request_id,
                        output_number=output_number)) or {}
                outputs.append(
                    self.pb2.OutputProgress(
                        id=output_number,
                        status=self.pb2.OutputStatus.Value(
                            output_status),
                        **progress))
        return outputs

    def _inputs(self, request_id, total_inputs):
        inputs: list[streaming_pb2.InputProgress] = []
        for input_number in range(total_inputs):
            input_status: str = self.cache.get(
                CacheKeysTemplates.INPUT_STATUS.format(
                    request_id=request_id,
                    input_number=input_number),
                decode=False)
            if input_status:
                progress: dict = self.cache.get(
                    CacheKeysTemplates.INPUT_DOWNLOADING_PROGRESS.format(
                        request_id=request_id,
                        input_number=input_number)) or {}
                inputs.append(
                    self.pb2.InputProgress(
                        id=input_number,
                        status=self.pb2.InputStatus.Value(
                            input_status),
                        **progress))
        return inputs
