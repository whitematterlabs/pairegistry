"""Wake-word + VAD primitives. Thin wrapper around openWakeWord and webrtcvad.

openWakeWord wants 16kHz int16 mono, 1280-frame blocks (80ms).
webrtcvad wants 10/20/30ms frames. We use 30ms frames (480 samples @16kHz).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 16000
WAKE_BLOCK = 1280            # samples — openWakeWord's expected window
VAD_FRAME_MS = 30
VAD_FRAME = SAMPLE_RATE * VAD_FRAME_MS // 1000  # 480 samples


@dataclass
class WakeHit:
    model: str
    score: float


class WakeDetector:
    """Wraps openwakeword.Model. Pass int16 mono frames; get hits."""

    def __init__(self, model_names: list[str], threshold: float = 0.5):
        from openwakeword.model import Model
        # ONNX framework — tflite-runtime has no Py3.14 wheel; onnxruntime does.
        self.model = Model(wakeword_models=model_names, inference_framework="onnx")
        self.threshold = threshold
        self.model_names = model_names

    def feed(self, frame_int16: np.ndarray) -> WakeHit | None:
        """Feed one WAKE_BLOCK-sized frame; returns a hit if any model fires."""
        scores = self.model.predict(frame_int16)
        for name, score in scores.items():
            if score >= self.threshold:
                return WakeHit(model=name, score=float(score))
        return None


class SilenceDetector:
    """webrtcvad-backed end-of-utterance detector.

    Call `is_speech(frame_bytes_30ms)` per 30ms; track trailing silence.
    """

    def __init__(self, aggressiveness: int = 2, silence_ms: int = 1000):
        import webrtcvad
        self.vad = webrtcvad.Vad(aggressiveness)
        self.silence_ms = silence_ms
        self.trailing_ms = 0

    def reset(self) -> None:
        self.trailing_ms = 0

    def is_done(self, frame_bytes: bytes) -> bool:
        """Returns True once trailing silence exceeds silence_ms."""
        if len(frame_bytes) != VAD_FRAME * 2:
            # webrtcvad strict on frame size; if upstream gives us a different
            # block size, treat as inconclusive (not silence).
            return False
        speech = self.vad.is_speech(frame_bytes, SAMPLE_RATE)
        if speech:
            self.trailing_ms = 0
        else:
            self.trailing_ms += VAD_FRAME_MS
        return self.trailing_ms >= self.silence_ms
