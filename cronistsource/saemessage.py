import numpy as np
from prometheus_client import Summary
from visionapi.common_pb2 import MessageType
from visionapi.sae_pb2 import SaeMessage

PROTO_SERIALIZATION_DURATION = Summary('cronist_source_proto_serialization_duration', 'The time it takes to create a serialized output proto')


@PROTO_SERIALIZATION_DURATION.time()
def to_sae_message(source_id: str, timestamp_utc_ms: int, image_bgr: np.ndarray, jpeg_bytes: bytes) -> bytes:
    '''Build the same message shape the live video source publishes (see video-source-py).'''
    msg = SaeMessage()
    msg.frame.source_id = source_id
    msg.frame.timestamp_utc_ms = timestamp_utc_ms
    msg.frame.shape.height = image_bgr.shape[0]
    msg.frame.shape.width = image_bgr.shape[1]
    msg.frame.shape.channels = image_bgr.shape[2]
    msg.frame.frame_data_jpeg = jpeg_bytes
    msg.type = MessageType.SAE

    return msg.SerializeToString()
