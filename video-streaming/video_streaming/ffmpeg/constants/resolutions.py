from ffmpeg_streaming import Representation, Size, Bitrate


class Resolutions:

    R_144P = "144p"
    R_240P = "240p"
    R_360P = "360p"
    R_480P = "480p"
    R_720P = "720p"
    R_1080P = "1080p"
    R_2K = "2k"
    R_4K = "4k"

    QUALITIES = {
        R_144P: Representation(
            Size(256, 144),
            Bitrate(95 * 1024, 64 * 1024)),
        R_240P: Representation(
            Size(426, 240),
            Bitrate(150 * 1024, 94 * 1024)),
        R_360P: Representation(
            Size(640, 360),
            Bitrate(276 * 1024, 128 * 1024)),
        R_480P: Representation(
            Size(854, 480),
            Bitrate(750 * 1024, 192 * 1024)),
        R_720P: Representation(
            Size(1280, 720),
            Bitrate(2048 * 1024, 320 * 1024)),
        R_1080P: Representation(
            Size(1920, 1080),
            Bitrate(4096 * 1024, 320 * 1024)),
        R_2K: Representation(
            Size(2560, 1440),
            Bitrate(6144 * 1024, 320 * 1024)),
        R_4K: Representation(
            Size(3840, 2160),
            Bitrate(17408 * 1024, 320 * 1024))
    }

    def get_reps(
            self,
            names: list[str]):
        reps = []
        for name in names:
            # skip empty strings
            if name and not name.isspace():
                try:
                    reps.append(self.QUALITIES[name])
                except KeyError:
                    raise KeyError(f"{name} is not a valid quality name.")
        return reps


