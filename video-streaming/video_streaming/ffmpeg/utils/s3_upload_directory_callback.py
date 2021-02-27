

class S3UploadDirectoryCallback(object):
    def __init__(self):
        self.uploaded = 0

    def progress(self, total_size, total_files, number, chunk):
        self.uploaded += chunk
        bytes_percent = self.uploaded / total_size * 100
        files_percent = number / total_files * 100

        print(f"uploaded files = {files_percent}% , uploaded = {bytes_percent}%")
        # TODO

