"""Notetaker audio capture: system audio via a Core Audio process tap +
the owner's mic via sounddevice, recorded to two raw PCM files (crash-safe:
a dead process leaves valid PCM streams; headers are ffmpeg's problem at
finalize time).

Probed 2026-07-07: the tap + aggregate device are created via pyobjc
(CATapDescription / AudioHardwareCreateProcessTap /
AudioHardwareCreateAggregateDevice with composition keys name/uid/private/
taps/tapautostart), but tap audio is only visible inside a HAL IOProc and
pyobjc cannot round-trip AudioDeviceIOProcID — so the IO leg is ctypes
(AudioDeviceCreateIOProcID + AudioDeviceStart). Tap format here: lpcm
float32 interleaved, 48kHz, 2ch.

Creating the tap requires the System Audio Recording permission (grouped
under Screen & System Audio Recording in Privacy & Security); a nonzero
OSStatus from AudioHardwareCreateProcessTap is surfaced as TapPermissionError.
"""
import ctypes
import threading
from pathlib import Path

_CA_PATH = "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"

AudioObjectID = ctypes.c_uint32
OSStatus = ctypes.c_int32


class AudioBuffer(ctypes.Structure):
    _fields_ = [
        ("mNumberChannels", ctypes.c_uint32),
        ("mDataByteSize", ctypes.c_uint32),
        ("mData", ctypes.c_void_p),
    ]


class AudioBufferList(ctypes.Structure):
    _fields_ = [
        ("mNumberBuffers", ctypes.c_uint32),
        ("mBuffers", AudioBuffer * 8),  # view; honor mNumberBuffers
    ]


class AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    ]


class ASBD(ctypes.Structure):
    _fields_ = [
        ("mSampleRate", ctypes.c_double),
        ("mFormatID", ctypes.c_uint32),
        ("mFormatFlags", ctypes.c_uint32),
        ("mBytesPerPacket", ctypes.c_uint32),
        ("mFramesPerPacket", ctypes.c_uint32),
        ("mBytesPerFrame", ctypes.c_uint32),
        ("mChannelsPerFrame", ctypes.c_uint32),
        ("mBitsPerChannel", ctypes.c_uint32),
        ("mReserved", ctypes.c_uint32),
    ]


_IOProc = ctypes.CFUNCTYPE(
    OSStatus,
    AudioObjectID,
    ctypes.c_void_p,                    # inNow
    ctypes.POINTER(AudioBufferList),    # inInputData
    ctypes.c_void_p,                    # inInputTime
    ctypes.c_void_p,                    # outOutputData
    ctypes.c_void_p,                    # inOutputTime
    ctypes.c_void_p,                    # clientData
)


def _fourcc(s: bytes) -> int:
    return int.from_bytes(s, "big")


class TapPermissionError(RuntimeError):
    """AudioHardwareCreateProcessTap refused — almost always the missing
    System Audio Recording grant."""


class TapCapture:
    """System-audio capture via process tap → raw float32 PCM file."""

    def __init__(self, out_path: Path, uid_suffix: str):
        self.out_path = out_path
        self.uid_suffix = uid_suffix
        self.sample_rate: float = 48000.0
        self.channels: int = 2
        self._ca = ctypes.CDLL(_CA_PATH)
        self._file = None
        self._lock = threading.Lock()
        self._tap_id = None
        self._agg_id = None
        self._proc_id = None
        self._io_proc = None  # keep the CFUNCTYPE object alive

    def start(self) -> None:
        import CoreAudio as CA

        desc = CA.CATapDescription.alloc().initStereoGlobalTapButExcludeProcesses_([])
        desc.setName_("PAI Notetaker Tap")
        uuid = str(desc.UUID().UUIDString())
        status, tap_id = CA.AudioHardwareCreateProcessTap(desc, None)
        if status != 0:
            raise TapPermissionError(
                f"AudioHardwareCreateProcessTap failed (status={status}) — "
                "grant System Audio Recording in System Settings > Privacy & "
                "Security > Screen & System Audio Recording"
            )
        self._tap_id = tap_id
        status2, agg_id = CA.AudioHardwareCreateAggregateDevice(
            {
                "name": "PAI Notetaker",
                "uid": f"pai-notetaker-agg-{self.uid_suffix}",
                "private": True,
                "taps": [{"uid": uuid, "drift": True}],
                "tapautostart": True,
            },
            None,
        )
        if status2 != 0:
            CA.AudioHardwareDestroyProcessTap(tap_id)
            self._tap_id = None
            raise RuntimeError(f"aggregate device creation failed (status={status2})")
        self._agg_id = agg_id

        # Input stream format (float32 interleaved expected; recorded to meta).
        addr = AudioObjectPropertyAddress(_fourcc(b"sfmt"), _fourcc(b"inpt"), 0)
        asbd = ASBD()
        size = ctypes.c_uint32(ctypes.sizeof(asbd))
        err = self._ca.AudioObjectGetPropertyData(
            AudioObjectID(agg_id), ctypes.byref(addr), 0, None,
            ctypes.byref(size), ctypes.byref(asbd),
        )
        if err == 0 and asbd.mSampleRate > 0:
            self.sample_rate = float(asbd.mSampleRate)
            self.channels = int(asbd.mChannelsPerFrame) or 2

        self._file = self.out_path.open("wb")

        def io_proc(dev, now, in_abl, in_time, out_abl, out_time, client):
            try:
                if in_abl:
                    abl = in_abl.contents
                    for i in range(min(abl.mNumberBuffers, 8)):
                        buf = abl.mBuffers[i]
                        if buf.mData and buf.mDataByteSize:
                            raw = ctypes.string_at(buf.mData, buf.mDataByteSize)
                            with self._lock:
                                if self._file is not None:
                                    self._file.write(raw)
            except Exception:
                pass  # never raise into the HAL
            return 0

        self._io_proc = _IOProc(io_proc)
        proc_id = ctypes.c_void_p()
        err = self._ca.AudioDeviceCreateIOProcID(
            AudioObjectID(agg_id), self._io_proc, None, ctypes.byref(proc_id)
        )
        if err != 0:
            self._teardown_device()
            raise RuntimeError(f"AudioDeviceCreateIOProcID failed (status={err})")
        self._proc_id = proc_id
        err = self._ca.AudioDeviceStart(AudioObjectID(agg_id), proc_id)
        if err != 0:
            self._teardown_device()
            raise RuntimeError(f"AudioDeviceStart failed (status={err})")

    def stop(self) -> None:
        if self._agg_id is not None and self._proc_id is not None:
            self._ca.AudioDeviceStop(AudioObjectID(self._agg_id), self._proc_id)
            self._ca.AudioDeviceDestroyIOProcID(AudioObjectID(self._agg_id), self._proc_id)
            self._proc_id = None
        self._teardown_device()
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None

    def _teardown_device(self) -> None:
        import CoreAudio as CA

        if self._agg_id is not None:
            CA.AudioHardwareDestroyAggregateDevice(self._agg_id)
            self._agg_id = None
        if self._tap_id is not None:
            CA.AudioHardwareDestroyProcessTap(self._tap_id)
            self._tap_id = None


class MicCapture:
    """Mic capture via sounddevice default input → raw int16 mono PCM file.

    Unavailable/denied mic is non-fatal (spec: record system audio only and
    note the owner's side is missing)."""

    def __init__(self, out_path: Path):
        self.out_path = out_path
        self.sample_rate: float = 16000.0
        self.channels: int = 1
        self._stream = None
        self._file = None

    def start(self) -> None:
        import sounddevice as sd

        info = sd.query_devices(kind="input")
        self.sample_rate = float(info["default_samplerate"] or 16000.0)
        self._file = self.out_path.open("wb")

        def cb(indata, frames, t, status):
            if self._file is not None:
                self._file.write(bytes(indata))

        self._stream = sd.RawInputStream(
            dtype="int16", channels=1, samplerate=int(self.sample_rate), callback=cb
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None
        if self._file is not None:
            self._file.close()
            self._file = None
