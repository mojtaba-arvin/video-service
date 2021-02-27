

class S3UploadedCallback(object):
    def __init__(self, file_size):

        self.uploaded = 0
        self._file_size = file_size

    def progress(self, chunk):
        self.uploaded += chunk
        percent = self.uploaded / self._file_size * 100
        print(percent)
        # TODO
