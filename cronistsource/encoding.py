import logging
from abc import ABC, abstractmethod

import numpy as np
from prometheus_client import Histogram
from turbojpeg import TurboJPEG

from .config import EncodingConfig

logger = logging.getLogger(__name__)

JPEG_ENCODE_DURATION = Histogram('cronist_source_jpeg_encode_duration_seconds', 'The time it takes to JPEG-encode one frame',
                                 buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25))
JPEG_BYTES = Histogram('cronist_source_jpeg_bytes', 'Size of the encoded JPEG payload per frame',
                       buckets=(25_000, 50_000, 100_000, 200_000, 400_000, 800_000, 1_600_000, 3_200_000))

class JpegEncoder(ABC):
    @abstractmethod
    def encode(self, image_bgr: np.ndarray) -> bytes:
        '''Encode a BGR image as JPEG.'''


class TurboJpegEncoder(JpegEncoder):
    '''libjpeg-turbo, the same encoder the live video source uses.'''

    def __init__(self, quality: int) -> None:
        self._jpeg = TurboJPEG()
        self._quality = quality

    def encode(self, image_bgr: np.ndarray) -> bytes:
        with JPEG_ENCODE_DURATION.time():
            data = self._jpeg.encode(image_bgr, quality=self._quality)
        JPEG_BYTES.observe(len(data))
        return data


def build_encoder(config: EncodingConfig) -> JpegEncoder:
    return TurboJpegEncoder(config.jpeg_quality)
