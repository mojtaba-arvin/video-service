from video_streaming.ffmpeg.tasks.base import BaseStreamingTask


class BaseCheckMixin(object):
    incr: BaseStreamingTask.incr

    primary_status: BaseStreamingTask.primary_status

    save_primary_status: BaseStreamingTask.save_primary_status
    get_job_details_by_request_id: BaseStreamingTask.get_job_details_by_request_id

    def incr_passed_checks(self):
        job_details = self.get_job_details_by_request_id()
        if job_details:
            passed_checks = self.incr("PASSED_CHECKS")
            if passed_checks == job_details['total_checks']:
                self.save_primary_status(
                    self.primary_status.CHECKS_FINISHED
                )
