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
            self,
            server)

    def create_job(self, request, context):
        """create a job, with multi outputs

        1. create empty tasks lists for the workflow
        2. generate unique uuid for the current request
        3. check input video parameter is not empty
        4. check input video is exist on the cloud as a first level task
            on the workflow
        5. check to not empty both playlists and thumbnails outputs
        6. check output keys are not empty and bucket names are
            compatible with s3
        7. check if output keys are exist to prevent upload risk
        8. check duplicate output locations in current request
        9. check unique output buckets are exist on the cloud or create
            them if has one create flag ,as first level tasks
        10. add third level tasks, e.g. download input
        11. initial processing tasks by output formats and chains them with upload task
        12. apply tasks
        """

        # 1. create empty tasks lists for the workflow

        # checks tasks before download anythings
        first_level: list = []
        second_level: list = []

        # download tasks, like target video, watermarks ...
        third_level: list = []

        # chains of process and upload tasks for every output
        fourth_level: list[chain] = []

        # delete local inputs and outputs files and call webhook
        fifth_level: list = []

        # 2.generate unique uuid for the current request
        request_id: str = str(uuid.uuid4())

        #
        webhook_url: str = request.webhook_url
        if webhook_url:
            # TODO check webhook_url is valid and raise error
            fifth_level.append(
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
        first_level.append(
            tasks.check_input_key.s(
                s3_input_key=s3_input_key,
                s3_input_bucket=s3_input_bucket,
                request_id=request_id
            ))

        # 5. check to not empty both playlists and thumbnails outputs
        if not request.playlists and not request.thumbnails:
            raise exceptions.OneOutputIsRequiredException

        # 6. check output keys are not empty and bucket names
        # are compatible with s3 and append check output key tasks to
        # second level
        output_buckets: list[str] = []
        output_locations: list[tuple] = []
        for outputs in [request.playlists, request.thumbnails]:
            for output in outputs:
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
                    second_level.append(
                        tasks.check_output_key.s(
                            s3_output_key=output.s3.key,
                            s3_output_bucket=output.s3.bucket,
                            s3_dont_replace=output.s3.dont_replace,
                            request_id=request_id
                        )
                    )

        # 8. check duplicate output locations in current request
        if len(output_locations) != len(set(output_locations)):
            raise exceptions.DuplicateOutputLocationsException

        # 9. check unique output buckets are exist on the cloud or
        # create them if has one create flag ,as first level tasks
        unique_output_buckets = list(set(output_buckets))
        # checking_output_buckets_tasks: list = []
        for bucket in unique_output_buckets:

            # search bucket has one create flag
            s3_create_bucket = self._has_create_flag(
                bucket, request.playlists) or self._has_create_flag(
                bucket, request.thumbnails)

            first_level.append(
                tasks.check_output_bucket.s(
                    s3_output_bucket=bucket,
                    s3_create_bucket=s3_create_bucket,
                    request_id=request_id
                )
            )

        # 10. add third level tasks, e.g. download input
        third_level.append(
            # chain of create playlist and upload_directory
            chain(
                # input object_details will come from second level
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
        fourth_level = self._append_playlists_tasks(
            request_id=request_id,
            playlists=request.playlists,
            append_to=fourth_level
        )

        fourth_level = self._append_thumbnails_tasks(
            request_id=request_id,
            thumbnails=request.thumbnails,
            append_to=fourth_level
        )

        reference_id: str = request.reference_id
        job_details = dict(
            reference_id=reference_id,
            webhook_url=webhook_url,
            total_checks=len(first_level)+len(second_level),
            total_inputs=len(third_level),
            total_outputs=len(fourth_level),
            total_playlists=len(request.playlists),
            total_thumbnails=len(request.thumbnails)
        )

        # saving job details
        self.cache.set(
            CacheKeysTemplates.JOB_DETAILS.format(
                request_id=request_id
            ),
            json.dumps(job_details))

        ordered_levels = [
            # chord 1
            first_level,
            # callback of chord 1
            second_level,

            # chord 2
            third_level,
            # callback of chard 2
            fourth_level,

            fifth_level
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
        self._raise_if_job_already_executed(
            primary_status,
            job_details)

        playlists_to_revoke: list[Streaming.pb2.OutputsToRevoke] = \
            self._outputs_to_revoke(
            request.playlists_numbers,
            primary_status,
            request_id,
            job_details['total_outputs'],
            CacheKeysTemplates.PLAYLIST_ID_PREFIX
        )

        thumbnails_to_revoke: list[Streaming.pb2.OutputsToRevoke] = \
            self._outputs_to_revoke(
            request.thumbnails_numbers,
            primary_status,
            request_id,
            job_details['total_outputs'],
            CacheKeysTemplates.THUMBNAIL_ID_PREFIX
        )

        response = self.pb2.RevokeOutputsResponse(
            tracking_id=request_id,
            reference_id=job_details['reference_id'],
            playlists_to_revoke=playlists_to_revoke,
            thumbnails_to_revoke=thumbnails_to_revoke
        )
        return response
