

class S3DownloadCallback(object):
    def __init__(self, object_size):

        self.downloaded = 0
        self._object_size = object_size

    def progress(self, chunk):
        self.downloaded += chunk
        percent = self.downloaded / self._object_size * 100
        print(percent)
        # TODO
