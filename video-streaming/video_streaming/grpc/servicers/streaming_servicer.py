import json
import uuid
from celery import chain
from video_streaming.cache import RedisCache
from video_streaming.core.constants import CacheKeysTemplates, \
    PrimaryStatus, OutputStatus
from video_streaming.core.services import S3Service
from video_streaming.ffmpeg import tasks
from video_streaming.grpc import exceptions
from video_streaming.grpc.protos import streaming_pb2_grpc, \
    streaming_pb2
from .mixins import CreateJobMixin, GetResultsMixin, RevokeJobsMixin, \
    RevokeOutputsMixin


class Streaming(
        RevokeOutputsMixin,
        RevokeJobsMixin,
        GetResultsMixin,
        CreateJobMixin,
        streaming_pb2_grpc.StreamingServicer):

    cache = RedisCache()
    pb2 = streaming_pb2

    def _add_to_server(self, server):
        streaming_pb2_grpc.add_StreamingServicer_to_server(
            self.__class__(),
            server)

    def create_job(self, request, context):
        """create a job, with multi outputs

        1. create empty tasks lists for the workflow
        2. generate unique uuid for the current request
        3. check input video parameter is not empty
        4. check input video is exist on the cloud as a first level task
            on the workflow
        5. check outputs list is empty
        6. check output keys are not empty and bucket names are
            compatible with s3
        7. check if output keys are exist to prevent upload risk
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
        # checking_upload_risk_tasks: list = []
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

            if output.s3.dont_replace:
                # 7. check if output keys are exist can be replace
                first_level_tasks.append(
                    tasks.check_output_key.s(
                        s3_output_key=output.s3.key,
                        s3_output_bucket=output.s3.bucket,
                        s3_dont_replace=output.s3.dont_replace,
                        request_id=request_id
                    )
                )
                # checking_upload_risk_tasks.append(
                #     tasks.check_output_key.s(
                #         s3_output_key=output.s3.key,
                #         s3_output_bucket=output.s3.bucket,
                #         s3_dont_replace=output.s3.dont_replace,
                #         request_id=request_id
                #     )
                # )

        # 8. check duplicate output locations in current request
        if len(output_locations) != len(set(output_locations)):
            raise exceptions.DuplicateOutputLocationsException

        # 9. check unique output buckets are exist on the cloud or
        # create them if has one create flag ,as first level tasks
        unique_output_buckets = list(set(output_buckets))
        # checking_output_buckets_tasks: list = []
        for bucket in unique_output_buckets:
            first_level_tasks.append(
                tasks.check_output_bucket.s(
                    s3_output_bucket=bucket,
                    s3_create_bucket=self.__class__._has_create_flag(
                        bucket, request.outputs),
                    request_id=request_id
                )
            )
            # checking_output_buckets_tasks.append(
            #     tasks.check_output_bucket.s(
            #         s3_output_bucket=bucket,
            #         s3_create_bucket=self.__class__._has_create_flag(
            #             bucket, request.outputs),
            #         request_id=request_id
            #     )
            # )

        # # check upload risk after checking buckets are exist
        # first_level_tasks.append(
        #     chain(
        #         group(*checking_output_buckets_tasks),
        #         group(*checking_upload_risk_tasks)
        #     )
        # )

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
        third_level_tasks = self._append_tasks(
            request_id=request_id,
            outputs=request.outputs,
            append_to=third_level_tasks
        )

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

        # 12. apply tasks
        self._apply_job(request_id, ordered_levels)
        return self._job_response(request_id)

    def get_results(self, request, context):
        """get results for a list of jobs"""
        results: list[Streaming.pb2.ResultDetails] = []
        for request_id in request.tracking_ids:
            result = self._get_result(request_id)
            if result:
                results.append(result)
        response = self.pb2.JobsResultsResponse(results=results)
        return response

    def revoke_jobs(self, request, context):
        """force stop a list of jobs
        to kill job outputs processes and delete all local files
        """
        results: list[Streaming.pb2.RevokeDetails] = []
        for request_id in request.tracking_ids:
            result = self._revoke_job(request_id)
            if result:
                results.append(result)
        response = self.pb2.RevokeJobsResponse(results=results)
        return response

    def revoke_job_outputs(self, request, context):
        """force stop a list of outputs for one job"""

        request_id: str = request.tracking_id
        primary_status: str = self.cache.get(
            CacheKeysTemplates.PRIMARY_STATUS.format(
                request_id=request_id), decode=False)
        job_details: dict = self.cache.get(
            CacheKeysTemplates.JOB_DETAILS.format(
                request_id=request_id))

        # raise if job is already has been executed
        self.__class__._raise_if_job_already_executed(
            primary_status,
            job_details)

        outputs_to_revoke: list[Streaming.pb2.OutputsToRevoke] = []
        for output_number in request.output_numbers:
            # is output_number valid?
            if output_number not in range(job_details['total_outputs']):
                outputs_to_revoke.append(self.pb2.OutputsToRevoke(
                        output_number=output_number,
                        signal_failed_reason=self.pb2.
                        signalFailedReason.OUTPUT_NUMBER_IS_INVALID))
                continue
            # check job has any output status to know if output is
            # already executed and can not revoke it
            if primary_status == PrimaryStatus.OUTPUTS_PROGRESSING:

                output_status = self.cache.get(
                    CacheKeysTemplates.OUTPUT_STATUS.format(
                        request_id=request_id,
                        output_number=output_number),
                    decode=False)
                if output_status == OutputStatus.OUTPUT_FAILED:
                    outputs_to_revoke.append(self.pb2.OutputsToRevoke(
                        output_number=output_number,
                        signal_failed_reason=self.pb2.
                        signalFailedReason.OUTPUT_HAS_BEEN_FAILED
                    ))
                    continue
                if output_status == OutputStatus.UPLOADING_FINISHED:
                    outputs_to_revoke.append(self.pb2.OutputsToRevoke(
                        output_number=output_number,
                        signal_failed_reason=self.pb2.
                        signalFailedReason.OUTPUT_HAS_BEEN_UPLOADED
                    ))
                    continue
                if output_status == OutputStatus.PLAYLIST_UPLOADING:
                    # when playlist is uploading, it's not safe to stop
                    outputs_to_revoke.append(self.pb2.OutputsToRevoke(
                        output_number=output_number,
                        signal_failed_reason=self.
                        pb2.signalFailedReason.
                        OUTPUT_UPLOADING_COULD_NOT_BE_STOPPED
                    ))
                    continue

            # ok, output can revoke
            self._send_revoke_output_signal(request_id, output_number)
            outputs_to_revoke.append(self.pb2.OutputsToRevoke(
                    output_number=output_number,
                    signal_has_been_sent=True))

        response = self.pb2.RevokeOutputsResponse(
            tracking_id=request_id,
            reference_id=job_details['reference_id'],
            outputs_to_revoke=outputs_to_revoke
        )
        return response
