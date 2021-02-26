

class FfmpegCallback(object):

    def __init__(self):
        pass

    def progress(self, ffmpeg, duration, time_, time_left, process):
        percent = round(time_ / duration * 100)
        print(percent)
        # TODO
