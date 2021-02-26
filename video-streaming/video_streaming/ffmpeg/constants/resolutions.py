from ffmpeg_streaming import Representation, Size, Bitrate


class Resolutions:

    _144P = "144p"
    _240P = "240p"
    _360P = "360p"
    _480P = "480p"
    _720P = "720p"
    _1080P = "1080p"
    _2K = "2k"
    _4K = "4k"

    QUALITIES = {
        _144P: Representation(
            Size(256, 144),
            Bitrate(95 * 1024, 64 * 1024)),
        _240P: Representation(
            Size(426, 240),
            Bitrate(150 * 1024, 94 * 1024)),
        _360P: Representation(
            Size(640, 360),
            Bitrate(276 * 1024, 128 * 1024)),
        _480P: Representation(
            Size(854, 480),
            Bitrate(750 * 1024, 192 * 1024)),
        _720P: Representation(
            Size(1280, 720),
            Bitrate(2048 * 1024, 320 * 1024)),
        _1080P: Representation(
            Size(1920, 1080),
            Bitrate(4096 * 1024, 320 * 1024)),
        _2K: Representation(
            Size(2560, 1440),
            Bitrate(6144 * 1024, 320 * 1024)),
        _4K: Representation(
            Size(3840, 2160),
            Bitrate(17408 * 1024, 320 * 1024))
    }

    def get_reps(
            self,
            names: list):
        reps = []
        for name in names:
            try:
                reps.append(self.QUALITIES[name])
            except KeyError:
                raise KeyError(f"{name} is a valid quality name.")
        return reps


