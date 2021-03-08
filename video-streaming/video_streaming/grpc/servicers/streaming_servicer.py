import json
import uuid
from celery import chord, chain, group, result as celery_result
from video_streaming.core.constants import CacheKeysTemplates, \
    PrimaryStatus
from video_streaming.core.services import S3Service
from video_streaming.ffmpeg import tasks
from video_streaming.grpc import exceptions
from video_streaming.grpc.exceptions import \
    DuplicateOutputLocationsException
from video_streaming.grpc.mixins import BaseGrpcServiceMixin
from video_streaming.grpc.protos import streaming_pb2_grpc


class Streaming(
        BaseGrpcServiceMixin,
        streaming_pb2_grpc.StreamingServicer):

    def video_processor(self, request, context):
        """

        1. create empty tasks lists for the workflow
        2. generate unique uuid for the current request
        3. check input video parameter is not empty
        4. check input video is exist on the cloud as a first level task
            on the workflow
        5. check outputs list is empty
        6. check output keys are not empty and bucket names are
            compatible with s3
        7. check if output keys are exist can be replace as first level
            tasks
        8. check duplicate output locations in current request
        9. check unique output buckets are exist on the cloud or create
            them if has one create flag ,as first level tasks
        10. add second level tasks, e.g. download input
        11. initial processing tasks by output formats and chains them with upload task
        12. apply tasks
        """

        # 1. create empty tasks lists for the workflow

        # some checks tasks before download anythings
        first_level_tasks: list = []
        # some download tasks, like target video, watermarks ...
        second_level_tasks: list = []
        # some chains of create playlist and upload directory
        # for every output
        third_level_tasks: list[chain] = []
        # delete local inputs and outputs files and call webhook
        fourth_level_tasks: list = []

        # 2.generate unique uuid for the current request
        request_id: str = str(uuid.uuid4())

        #
        webhook_url: str = request.webhook_url
        if webhook_url:
            # TODO check webhook_url is valid and raise error
            fourth_level_tasks.append(
                # webhook_url value will come from job_details cache
                # by request id
                tasks.call_webhook.s(request_id=request_id)
            )

        # 3. check input video parameter is not empty

        s3_input_key: str = request.s3_input.key
        s3_input_bucket: str = request.s3_input.bucket

        # For strings in proto3, the default value is the empty string
        # check input key to not be empty string
        if s3_input_key.isspace():
            raise exceptions.S3KeyCanNotBeEmptyException

        # 4. check input video is exist on the cloud
        # as a first level task
        first_level_tasks.append(
            tasks.check_input_key.s(
                s3_input_key=s3_input_key,
                s3_input_bucket=s3_input_bucket,
                request_id=request_id
            ))

        # 5. check outputs list is empty
        if not request.outputs:
            raise exceptions.OneOutputIsRequiredException

        # 6. check output keys are not empty and bucket names
        # are compatible with s3
        output_buckets: list[str] = []
        output_locations: list[tuple] = []
        for output in request.outputs:

            # check the output key is filled
            if output.s3.key.isspace():
                raise exceptions.S3KeyCanNotBeEmptyException

            # when output bucket is filled and has create flag,
            # then check the output bucket is compatible with s3
            if output.s3.bucket and output.s3.create_bucket and \
                    not S3Service.validate_bucket_name(output.s3.bucket):
                raise exceptions.BucketNameIsNotValidException

            # to use for getting unique output buckets names
            output_buckets.append(output.s3.bucket)

            # to use for checking duplicate output locations
            output_locations.append((output.s3.bucket, output.s3.key))

            # 7. check if output keys are exist can be replace
            # as first level tasks
            first_level_tasks.append(
                tasks.check_output_key.s(
                    s3_output_key=output.s3.key,
                    s3_output_bucket=output.s3.bucket,
                    s3_dont_replace=output.s3.dont_replace,
                    request_id=request_id
                )
            )

        # 8. check duplicate output locations in current request
        if len(output_locations) != len(set(output_locations)):
            raise DuplicateOutputLocationsException

        # 9. check unique output buckets are exist on the cloud or
        # create them if has one create flag ,as first level tasks
        unique_output_buckets = list(set(output_buckets))
        for bucket in unique_output_buckets:
            first_level_tasks.append(
                tasks.check_output_bucket.s(
                    s3_output_bucket=bucket,
                    s3_create_bucket=self.__class__._has_create_flag(
                        bucket, request.outputs),
                    request_id=request_id
                ))

        # 10. add second level tasks, e.g. download input
        second_level_tasks.append(
            # chain of create playlist and upload_directory
            chain(
                # input object_details will come from first level
                tasks.download_input.s(
                    # object_details=object_details,
                    request_id=request_id,
                    s3_input_key=s3_input_key,
                    s3_input_bucket=s3_input_bucket,
                    input_number=0
                ),
                # input_path will come from download_input task
                tasks.analyze_input.s(
                    request_id=request_id,
                    input_number=0
                )
            )
        )

        # 11. initial processing tasks by output formats
        # and chains them with upload task
        for output_number, output in enumerate(request.outputs):

            encode_format, video_codec, audio_codec = self._get_format(
                output)
            quality_names: list[str] = self._parse_quality_names(
                output.options.quality_names)
            custom_qualities: list[dict] = self.__class__._parse_custom_qualities(
                output.options.custom_qualities)

            third_level_tasks.append(
                # chain of create playlist and upload_directory
                chain(
                    # input_path will come from second level
                    tasks.create_playlist.s(
                        s3_output_key=output.s3.key,
                        fragmented=output.options.fmp4,  # just for HLS type
                        encode_format=encode_format,
                        video_codec=video_codec,
                        audio_codec=audio_codec,
                        quality_names=quality_names,
                        custom_qualities=custom_qualities,
                        request_id=request_id,
                        output_number=output_number,
                        is_hls=output.protocol == self.pb2.Protocol.HLS
                    ),
                    # directory will come from create playlist task
                    tasks.upload_directory.s(
                        s3_output_key=output.s3.key,
                        s3_output_bucket=output.s3.bucket,
                        request_id=request_id,
                        output_number=output_number
                    )
                )
            )

        # TODO delete local files on failure callback

        ordered_levels = [
            # chord 1
            first_level_tasks,
            # callback of chord 1
            second_level_tasks,

            # chord 2
            third_level_tasks,
            # callback of chard 2
            fourth_level_tasks
        ]

        job = chain(*[group(*level_tasks) for level_tasks in ordered_levels])

        reference_id: str = request.reference_id
        job_details = dict(
            reference_id=reference_id,
            webhook_url=webhook_url,
            total_checks=len(first_level_tasks),
            total_inputs=len(second_level_tasks),
            total_outputs=len(third_level_tasks)
        )

        # saving job details
        self.cache.set(
            CacheKeysTemplates.JOB_DETAILS.format(
                request_id=request_id
            ),
            json.dumps(job_details))

        # set first primary status as QUEUING_CHECKS
        self.cache.set(
            CacheKeysTemplates.PRIMARY_STATUS.format(
                request_id=request_id),
            PrimaryStatus.QUEUING_CHECKS
        )

        # 12. apply tasks
        result = job.apply_async()

        # check last level has one task or more to detect result type
        if len(ordered_levels[-1]) > 1:
            result: celery_result.GroupResult
            result.save()

        # saving celery result id of the request
        self.cache.set(
            CacheKeysTemplates.REQUEST_RESULT_ID.format(
                request_id=request_id
            ),
            str(result.id)
        )

        response = self.pb2.JobResponse()
        response.tracking_id = request_id
        return response

    def get_results(self, request, context):
        results: list[Streaming.pb2.ResultDetails] = []
        for request_id in request.tracking_ids:
            # TODO set flag on proto to add INPUT_FFPROBE_DATA to the result
            primary_status: str = self.cache.get(
                CacheKeysTemplates.PRIMARY_STATUS.format(
                    request_id=request_id), decode=False)
            job_details: dict = self.cache.get(
                CacheKeysTemplates.JOB_DETAILS.format(
                    request_id=request_id))
            if primary_status and job_details:
                status = self.pb2.PrimaryStatus.Value(
                    primary_status)
                reference_id: str = job_details['reference_id']
                total_checks: int = job_details['total_checks']
                total_inputs: int = job_details['total_inputs']
                total_outputs: int = job_details['total_outputs']
                ready_outputs: int = self.cache.get(
                        CacheKeysTemplates.READY_OUTPUTS.format(
                            request_id=request_id)) or 0
                checks = self.pb2.Checks(
                    total=total_checks,
                    passed=self.cache.get(
                        CacheKeysTemplates.PASSED_CHECKS.format(
                            request_id=request_id)) or 0)
                result_details = dict(
                    request_id=request_id,
                    reference_id=reference_id,
                    status=status,
                    total_outputs=total_outputs,
                    ready_outputs=ready_outputs,
                    checks=checks,
                    inputs=self._inputs(request_id, total_inputs),
                    outputs=self._outputs(request_id, total_outputs)
                )
                results.append(self.pb2.ResultDetails(**result_details))
        response = self.pb2.ResultResponse(results=results)
        return response

    # TODO add revoke method
