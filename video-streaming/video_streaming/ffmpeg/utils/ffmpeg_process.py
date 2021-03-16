import ffmpeg
import six
import logging
import threading


class FfmpegProcess(object):
    out = None
    err = None

    def __init__(self,
                 process,
                 progress_callback: callable = None,
                 timeout: float = None):
        """

        Args:
            process ():
            monitor ():
            timeout (): timeout for the operation in seconds
        """
        self.timeout = timeout
        self.process = process
        self.has_callback = callable(progress_callback)
        self.progress_callback = progress_callback

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_traceback):
        self.process.kill()
        if exc_type and isinstance(ffmpeg.Error, exc_type):
            six.reraise(exc_type, exc_value, exc_traceback)

    def run(self):
        if self.has_callback:
            thread = threading.Thread(target=self._target)

            # Start the thread's activity.
            thread.start()

            # Wait until the thread terminates.
            thread.join(timeout=self.timeout)

            # callis_alive() after join() to decide whether a timeout happened
            if thread.is_alive():
                self.process.terminate()
                thread.join()
                error = f'Timeout! {self.timeout} seconds.'
                logging.error(error)
                raise RuntimeError(error)
        else:
            FfmpegProcess.out, FfmpegProcess.err = self.process.communicate(
                timeout=self.timeout)

        # Check if child process has terminated.
        # set and return returncode attribute.
        retcode = self.process.poll()
        if retcode:
            error = str(FfmpegProcess.err) if FfmpegProcess.err else str(FfmpegProcess.out)
            logging.error(f'ffmpeg failed: {error}')
            raise Exception(error)

        logging.info("ffmpeg executed successfully")
        return FfmpegProcess.out, FfmpegProcess.err

    def _target(self):
        log = []
        while True:
            line = self.process.stdout.readline().strip()
            if line == '' and self.process.poll() is not None:
                break
            if line != '':
                log += [line]
            if callable(self.progress_callback):
                self.progress_callback(line, self.process)
        FfmpegProcess.out = log
