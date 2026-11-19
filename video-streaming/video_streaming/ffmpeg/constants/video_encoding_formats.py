from ffmpeg_streaming import Formats


class VideoEncodingFormats(Formats):
    H264 = "h264"
    HEVC = "hevc"
    VP9 = "vp9"

    def get_format_class(
            self,
            name: str,
            video: str = None,
            audio: str = None,
            **codec_options):
        if video is not None:
            codec_options['video'] = video
        if audio is not None:
            codec_options['audio'] = audio

        formats_dict = {
            self.H264: self.h264(**codec_options),
            self.HEVC: self.hevc(**codec_options),
            self.VP9: self.vp9(**codec_options)
        }
        if name is None:
            name = VideoEncodingFormats.H264
        try:
            return formats_dict[name]
        except KeyError:
            raise KeyError(f"`{name}` video encoding format not supported or invalid")
