from __future__ import annotations

import importlib.util
import json
import os
import queue
import threading
import wave
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from aTrain_core.globals import ATRAIN_DIR

LiveStatus = Literal["idle", "ready", "recording", "paused", "finalized", "blocked"]
SegmentStatus = Literal["queued", "transcribing", "committed", "corrected"]

LIVE_SESSION_DIR = ATRAIN_DIR / "live_sessions"
LIVE_SESSION_FILENAME = "session_state.json"
LIVE_MASTER_RECORDING = "master_recording.wav"
SECOND_SOURCE_RECORDING = "second_source.wav"
FLUSH_SENTINEL = object()


@dataclass(slots=True)
class RuntimeCapability:
    name: str
    available: bool
    detail: str


@dataclass(slots=True)
class LiveSegment:
    segment_id: str
    audio_start: float
    audio_end: float
    draft_text: str = ""
    final_text: str = ""
    speaker_id_provisional: int | None = None
    speaker_id_final: int | None = None
    status: SegmentStatus = "queued"
    ui_side: str = "self-start"
    ui_color: str = "#F3F4F6"


@dataclass(slots=True)
class LiveSessionSnapshot:
    session_id: str
    created_at: str
    updated_at: str
    status: LiveStatus
    audio_input: str
    second_source: str
    model: str | None
    speaker_mode: str
    language_mode: str
    language: str | None
    master_recording_path: str
    state_path: str
    segments: list[LiveSegment] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["segments"] = [asdict(segment) for segment in self.segments]
        return payload


@dataclass(slots=True)
class LiveSessionConfig:
    audio_input: str
    second_source: str
    model: str | None
    speaker_mode: str
    language_mode: str
    language: str | None
    device: str = "cpu"
    compute_type: str = "int8"
    temperature: float | None = None
    initial_prompt: str | None = None


@dataclass(slots=True)
class SegmentJob:
    segment_id: str
    audio_start: float
    audio_end: float
    audio_path: str


class LiveSessionStore:
    def __init__(self, base_dir: Path = LIVE_SESSION_DIR) -> None:
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def create_session(
        self, config: LiveSessionConfig, *, blocked_reason: str | None = None
    ) -> LiveSessionSnapshot:
        session_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        session_id = f"live_{session_stamp}_{uuid4().hex[:8]}"
        session_dir = self.base_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "chunks").mkdir(exist_ok=True)
        created_at = datetime.now(timezone.utc).isoformat()
        snapshot = LiveSessionSnapshot(
            session_id=session_id,
            created_at=created_at,
            updated_at=created_at,
            status="blocked" if blocked_reason else "ready",
            audio_input=config.audio_input,
            second_source=config.second_source,
            model=config.model,
            speaker_mode=config.speaker_mode,
            language_mode=config.language_mode,
            language=config.language,
            master_recording_path=str(session_dir / LIVE_MASTER_RECORDING),
            state_path=str(session_dir / LIVE_SESSION_FILENAME),
            notes=[blocked_reason] if blocked_reason else [],
        )
        self.write_snapshot(snapshot)
        return snapshot

    def write_snapshot(self, snapshot: LiveSessionSnapshot) -> None:
        snapshot.updated_at = datetime.now(timezone.utc).isoformat()
        with open(snapshot.state_path, "w", encoding="utf-8") as handle:
            json.dump(snapshot.to_dict(), handle, indent=2, ensure_ascii=False)


class LiveInterviewService:
    def __init__(self, store: LiveSessionStore | None = None) -> None:
        self.store = store or LiveSessionStore()
        self.capabilities = detect_live_runtime()
        self.current_session: LiveSessionSnapshot | None = None
        self.current_config: LiveSessionConfig | None = None
        self.sample_rate = 16000
        self.block_size = 1600
        self.energy_threshold = 0.012
        self.min_speech_seconds = 0.9
        self.max_segment_seconds = 12.0
        self.silence_hold_seconds = 0.6
        self.overlap_seconds = 0.35
        self.stream = None
        self.second_stream = None
        self._master_wave: wave.Wave_write | None = None
        self._second_wave: wave.Wave_write | None = None
        self._stop_event = threading.Event()
        self._capture_paused = threading.Event()
        self._state_lock = threading.Lock()
        self._audio_queue: queue.Queue = queue.Queue(maxsize=256)
        self._segment_queue: queue.Queue = queue.Queue(maxsize=64)
        self._threads: list[threading.Thread] = []
        self._speaker_profiles: list[tuple[int, list[float]]] = []
        self._whisper_model = None
        self._pending_export_thread: threading.Thread | None = None

    def get_current_snapshot(self) -> dict[str, Any] | None:
        if self.current_session is None:
            return None
        with self._state_lock:
            return self.current_session.to_dict()

    def missing_runtime_dependencies(self) -> list[RuntimeCapability]:
        return [capability for capability in self.capabilities if not capability.available]

    def can_record(self) -> bool:
        required = {"sounddevice", "numpy", "faster_whisper"}
        available = {capability.name for capability in self.capabilities if capability.available}
        return required.issubset(available)

    def start_session(self, config: LiveSessionConfig) -> LiveSessionSnapshot:
        if self.current_session is not None and self.current_session.status == "recording":
            return self.current_session
        if self.current_session is not None and self.current_session.status == "paused":
            self.current_session.status = "recording"
            self._capture_paused.clear()
            self._note("Session resumed from pause.")
            self._write_state()
            return self.current_session

        blocked_reason = None
        if not self.can_record():
            missing = ", ".join(capability.name for capability in self.missing_runtime_dependencies())
            blocked_reason = f"Live recording backend unavailable in this runtime. Missing: {missing}."
        self._flush_audio_queue()
        while not self._segment_queue.empty():
            try:
                self._segment_queue.get_nowait()
            except queue.Empty:
                break
        self._speaker_profiles = []
        self.current_config = config
        snapshot = self.store.create_session(config, blocked_reason=blocked_reason)
        self.current_session = snapshot
        if blocked_reason is not None:
            return snapshot

        self._stop_event.clear()
        self._capture_paused.clear()
        self._threads = [
            threading.Thread(target=self._segment_worker, name="live-segmenter", daemon=True),
            threading.Thread(target=self._asr_worker, name="live-asr", daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        try:
            self._open_capture()
        except Exception as exc:  # noqa: BLE001
            snapshot.status = "blocked"
            self._note(f"Failed to open audio input: {exc}")
            self._stop_event.set()
            self._capture_paused.set()
            self._close_streams()
            self._threads.clear()
            self._write_state()
            return snapshot
        snapshot.status = "recording"
        self._note("Live recording started.")
        self._write_state()
        return snapshot

    def pause_session(self) -> LiveSessionSnapshot | None:
        if self.current_session is None:
            return None
        self.current_session.status = "paused"
        self._capture_paused.set()
        self._request_segment_flush()
        self._note("Session paused from the live interview control panel.")
        self._write_state()
        return self.current_session

    def finalize_session(self) -> LiveSessionSnapshot | None:
        if self.current_session is None:
            return None
        self._request_segment_flush()
        self._stop_event.set()
        self._capture_paused.set()
        self._close_streams()
        for thread in self._threads:
            thread.join(timeout=2)
        self._threads.clear()
        self._flush_audio_queue()
        self.current_session.status = "finalized"
        self._note("Live capture stopped. Running final export pipeline.")
        self._write_state()
        self._run_final_export()
        return self.current_session

    def list_audio_inputs(self) -> dict[str, str]:
        if importlib.util.find_spec("sounddevice") is None:
            return {"default": "Default system microphone"}
        import sounddevice as sd

        devices: dict[str, str] = {"default": "Default system microphone"}
        for index, device in enumerate(sd.query_devices()):
            if device.get("max_input_channels", 0) > 0:
                devices[f"{index}:{device['name']}"] = f"{device['name']} ({device['hostapi']})"
        return devices

    def _resolve_device(self, value: str | None):
        if value in (None, "default"):
            return None
        try:
            return int(str(value).split(":", 1)[0])
        except (TypeError, ValueError):
            return None

    def _request_segment_flush(self) -> None:
        try:
            self._audio_queue.put_nowait(FLUSH_SENTINEL)
        except queue.Full:
            pass

    def _close_streams(self) -> None:
        for stream in [self.stream, self.second_stream]:
            if stream is not None:
                try:
                    stream.stop()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass
        self.stream = None
        self.second_stream = None
        for handle_name in ["_master_wave", "_second_wave"]:
            handle = getattr(self, handle_name)
            if handle is not None:
                handle.close()
                setattr(self, handle_name, None)

    def _open_capture(self) -> None:
        import numpy as np
        import sounddevice as sd

        if self.current_session is None or self.current_config is None:
            raise RuntimeError("Cannot open capture without an active session.")

        session_dir = Path(self.current_session.master_recording_path).parent
        self._master_wave = wave.open(str(session_dir / LIVE_MASTER_RECORDING), "wb")
        self._master_wave.setnchannels(1)
        self._master_wave.setsampwidth(2)
        self._master_wave.setframerate(self.sample_rate)

        primary_device = self._resolve_device(self.current_config.audio_input)
        second_device = self._resolve_device(self.current_config.second_source)
        if second_device == primary_device:
            second_device = None

        def callback(indata, frames, time_info, status) -> None:
            if self._stop_event.is_set() or self._capture_paused.is_set():
                return
            if status:
                self._note(f"Capture status: {status}")
            mono = np.squeeze(indata.copy()).astype("float32")
            try:
                self._audio_queue.put_nowait(mono)
            except queue.Full:
                self._note("Audio queue full; dropping live frame.")
                return
            int16_audio = np.clip(mono * 32767, -32768, 32767).astype("int16")
            if self._master_wave is not None:
                self._master_wave.writeframes(int16_audio.tobytes())

        self.stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            blocksize=self.block_size,
            callback=callback,
            dtype="float32",
            device=primary_device,
        )
        self.stream.start()

        if second_device is not None:
            self._second_wave = wave.open(str(session_dir / SECOND_SOURCE_RECORDING), "wb")
            self._second_wave.setnchannels(1)
            self._second_wave.setsampwidth(2)
            self._second_wave.setframerate(self.sample_rate)

            def second_callback(indata, frames, time_info, status) -> None:
                if self._stop_event.is_set() or self._capture_paused.is_set():
                    return
                if status:
                    self._note(f"Second source status: {status}")
                mono = np.squeeze(indata.copy()).astype("float32")
                int16_audio = np.clip(mono * 32767, -32768, 32767).astype("int16")
                if self._second_wave is not None:
                    self._second_wave.writeframes(int16_audio.tobytes())

            self.second_stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                blocksize=self.block_size,
                callback=second_callback,
                dtype="float32",
                device=second_device,
            )
            self.second_stream.start()
            self._note("Second source capture started for archival recording.")

    def _segment_worker(self) -> None:
        import numpy as np

        speech_buffers: list[Any] = []
        overlap_buffers: list[Any] = []
        silence_frames = 0
        segment_start: float | None = None
        processed_frames = 0
        min_frames = int(self.min_speech_seconds * self.sample_rate)
        max_frames = int(self.max_segment_seconds * self.sample_rate)
        silence_limit = int(self.silence_hold_seconds * self.sample_rate)
        overlap_frames = int(self.overlap_seconds * self.sample_rate)

        while not self._stop_event.is_set() or not self._audio_queue.empty():
            try:
                frame = self._audio_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            if frame is FLUSH_SENTINEL:
                if speech_buffers and segment_start is not None:
                    segment_audio = np.concatenate(speech_buffers)
                    segment_end = processed_frames / self.sample_rate
                    self._queue_segment(segment_audio, segment_start, segment_end)
                    speech_buffers = []
                    overlap_buffers = []
                    segment_start = None
                    silence_frames = 0
                continue

            processed_frames += len(frame)
            energy = float(np.sqrt(np.mean(np.square(frame))))
            is_speech = energy >= self.energy_threshold
            overlap_buffers.append(frame)
            overlap_buffers = _trim_frames(overlap_buffers, overlap_frames)

            if is_speech:
                if segment_start is None:
                    segment_start = max(0.0, (processed_frames - len(frame)) / self.sample_rate)
                    speech_buffers = [buf.copy() for buf in overlap_buffers[:-1]] + [frame]
                else:
                    speech_buffers.append(frame)
                silence_frames = 0
            elif segment_start is not None:
                speech_buffers.append(frame)
                silence_frames += len(frame)

            current_frames = sum(len(buf) for buf in speech_buffers)
            should_flush = False
            if segment_start is not None:
                if current_frames >= max_frames:
                    should_flush = True
                elif current_frames >= min_frames and silence_frames >= silence_limit:
                    should_flush = True

            if should_flush and segment_start is not None:
                segment_audio = np.concatenate(speech_buffers)
                segment_end = processed_frames / self.sample_rate
                self._queue_segment(segment_audio, segment_start, segment_end)
                speech_buffers = [buf.copy() for buf in overlap_buffers]
                segment_start = None
                silence_frames = 0

        if speech_buffers and segment_start is not None:
            segment_audio = np.concatenate(speech_buffers)
            segment_end = processed_frames / self.sample_rate
            self._queue_segment(segment_audio, segment_start, segment_end)

    def _queue_segment(self, audio, audio_start: float, audio_end: float) -> None:
        if self.current_session is None:
            return
        segment_id = f"seg_{len(self.current_session.segments) + 1:05d}"
        segment_path = self._write_segment_audio(segment_id, audio)
        segment = LiveSegment(
            segment_id=segment_id,
            audio_start=audio_start,
            audio_end=audio_end,
            status="transcribing",
        )
        with self._state_lock:
            self.current_session.segments.append(segment)
            self.store.write_snapshot(self.current_session)
        self._segment_queue.put(
            SegmentJob(
                segment_id=segment_id,
                audio_start=audio_start,
                audio_end=audio_end,
                audio_path=str(segment_path),
            )
        )

    def _write_segment_audio(self, segment_id: str, audio) -> Path:
        import numpy as np

        if self.current_session is None:
            raise RuntimeError("No active session for segment audio.")
        session_dir = Path(self.current_session.master_recording_path).parent / "chunks"
        session_dir.mkdir(exist_ok=True)
        segment_path = session_dir / f"{segment_id}.wav"
        int16_audio = np.clip(audio * 32767, -32768, 32767).astype("int16")
        with wave.open(str(segment_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(self.sample_rate)
            handle.writeframes(int16_audio.tobytes())
        return segment_path

    def _asr_worker(self) -> None:
        while not self._stop_event.is_set() or not self._segment_queue.empty():
            try:
                job = self._segment_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                text = self._transcribe_segment(job.audio_path)
                speaker_id = self._assign_speaker(job.audio_path)
                self._commit_segment(job.segment_id, text, speaker_id)
            except Exception as exc:  # noqa: BLE001
                self._note(f"ASR failure on {job.segment_id}: {exc}")
                self._commit_segment(job.segment_id, "", 1)

    def _transcribe_segment(self, audio_path: str) -> str:
        from faster_whisper import WhisperModel

        if self.current_config is None:
            raise RuntimeError("No live session config available for transcription.")
        if self._whisper_model is None:
            self._whisper_model = WhisperModel(
                self.current_config.model or "small",
                device=self.current_config.device,
                compute_type=self.current_config.compute_type,
            )
        language = None if self.current_config.language_mode == "auto" else self.current_config.language
        segments, _info = self._whisper_model.transcribe(
            audio_path,
            language=language,
            beam_size=1,
            best_of=1,
            condition_on_previous_text=True,
            initial_prompt=self.current_config.initial_prompt,
            temperature=self.current_config.temperature or 0.0,
            vad_filter=False,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return text

    def _assign_speaker(self, audio_path: str) -> int:
        import numpy as np

        max_speakers = {"two": 2, "four": 4}.get(
            self.current_config.speaker_mode if self.current_config else "auto", 4
        )
        with wave.open(audio_path, "rb") as handle:
            frames = handle.readframes(handle.getnframes())
        audio = np.frombuffer(frames, dtype=np.int16).astype("float32") / 32768.0
        features = _extract_voice_features(audio, self.sample_rate)
        if not self._speaker_profiles:
            self._speaker_profiles.append((1, features))
            return 1

        best_speaker = None
        best_distance = float("inf")
        for speaker_id, centroid in self._speaker_profiles:
            distance = _feature_distance(features, centroid)
            if distance < best_distance:
                best_distance = distance
                best_speaker = speaker_id

        if best_speaker is not None and best_distance < 0.18:
            self._speaker_profiles = [
                (speaker_id, _average_features(centroid, features) if speaker_id == best_speaker else centroid)
                for speaker_id, centroid in self._speaker_profiles
            ]
            return best_speaker

        if len(self._speaker_profiles) < max_speakers:
            speaker_id = len(self._speaker_profiles) + 1
            self._speaker_profiles.append((speaker_id, features))
            return speaker_id
        return best_speaker or 1

    def _commit_segment(self, segment_id: str, text: str, speaker_id: int) -> None:
        if self.current_session is None:
            return
        side, color = _speaker_style(speaker_id)
        with self._state_lock:
            for segment in self.current_session.segments:
                if segment.segment_id == segment_id:
                    segment.final_text = text
                    segment.status = "committed"
                    segment.speaker_id_provisional = speaker_id
                    segment.ui_side = side
                    segment.ui_color = color
                    break
            self.store.write_snapshot(self.current_session)

    def _run_final_export(self) -> None:
        if self.current_session is None or self.current_config is None:
            return

        def runner() -> None:
            try:
                from aTrain_core.settings import ComputeType, Device, Settings
                from aTrain_core.transcribe import prepare_transcription, transcribe

                with open(self.current_session.master_recording_path, "rb") as audio_file:
                    _, file_id, timestamp = prepare_transcription(
                        Path(self.current_session.master_recording_path)
                    )
                    settings = Settings(
                        file=audio_file,
                        file_id=file_id,
                        file_name=os.path.basename(self.current_session.master_recording_path),
                        model=self.current_config.model,
                        language=None
                        if self.current_config.language_mode == "auto"
                        else self.current_config.language,
                        speaker_detection=True,
                        speaker_count={"two": 2, "four": 4}.get(
                            self.current_config.speaker_mode
                        ),
                        device=Device.GPU
                        if self.current_config.device.lower() == "gpu"
                        else Device.CPU,
                        compute_type=ComputeType(self.current_config.compute_type),
                        timestamp=timestamp,
                        temperature=self.current_config.temperature,
                        initial_prompt=self.current_config.initial_prompt,
                        progress=None,
                    )
                    transcribe(settings=settings)
                self._note("Final export pipeline completed.")
            except Exception as exc:  # noqa: BLE001
                self._note(f"Final export pipeline failed: {exc}")
            finally:
                self._write_state()

        self._pending_export_thread = threading.Thread(
            target=runner, name="live-final-export", daemon=True
        )
        self._pending_export_thread.start()

    def _flush_audio_queue(self) -> None:
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except queue.Empty:
                break

    def _write_state(self) -> None:
        if self.current_session is not None:
            with self._state_lock:
                self.store.write_snapshot(self.current_session)

    def _note(self, message: str) -> None:
        if self.current_session is None:
            return
        with self._state_lock:
            timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
            self.current_session.notes.append(f"[{timestamp}] {message}")
            self.current_session.notes = self.current_session.notes[-50:]


def detect_live_runtime() -> list[RuntimeCapability]:
    checks = [
        ("sounddevice", "Local microphone capture"),
        ("numpy", "Audio frame buffering"),
        ("faster_whisper", "Incremental speech recognition"),
        ("pyannote.audio", "Speaker diarization refinement"),
    ]
    capabilities: list[RuntimeCapability] = []
    for module_name, purpose in checks:
        try:
            available = importlib.util.find_spec(module_name) is not None
        except ModuleNotFoundError:
            available = False
        detail = (
            f"{purpose} available"
            if available
            else f"Missing dependency for {purpose.lower()}"
        )
        capabilities.append(RuntimeCapability(module_name, available, detail))
    return capabilities


def _trim_frames(buffers: list, frame_limit: int) -> list:
    trimmed: list = []
    count = 0
    for buffer in reversed(buffers):
        trimmed.insert(0, buffer)
        count += len(buffer)
        if count >= frame_limit:
            break
    return trimmed


def _extract_voice_features(audio, sample_rate: int) -> list[float]:
    import numpy as np

    if len(audio) == 0:
        return [0.0, 0.0, 0.0, 0.0]
    rms = float(np.sqrt(np.mean(np.square(audio))))
    zcr = float(np.mean(np.abs(np.diff(np.sign(audio)))) / 2)
    spectrum = np.abs(np.fft.rfft(audio))
    if spectrum.sum() == 0:
        centroid = 0.0
        bandwidth = 0.0
    else:
        freqs = np.fft.rfftfreq(len(audio), 1 / sample_rate)
        centroid = float((freqs * spectrum).sum() / spectrum.sum()) / sample_rate
        bandwidth = float(np.sqrt((((freqs - centroid * sample_rate) ** 2) * spectrum).sum() / spectrum.sum())) / sample_rate
    return [rms, zcr, centroid, bandwidth]


def _feature_distance(left: list[float], right: list[float]) -> float:
    return sum(abs(a - b) for a, b in zip(left, right))


def _average_features(left: list[float], right: list[float]) -> list[float]:
    return [(a + b) / 2 for a, b in zip(left, right)]


def _speaker_style(speaker_id: int) -> tuple[str, str]:
    styles = {
        1: ("self-start", "#DCFCE7"),
        2: ("self-end", "#DBEAFE"),
        3: ("self-start", "#FFEDD5"),
        4: ("self-end", "#F3E8FF"),
    }
    return styles.get(speaker_id, styles[1])
