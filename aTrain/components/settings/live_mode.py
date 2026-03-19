from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta

from nicegui import app, ui

from aTrain.components.settings.language import get_language_options
from aTrain.components.settings.model import get_model_options
from aTrain.utils.live_interview import (
    LiveInterviewService,
    LiveSessionConfig,
    LiveSessionSnapshot,
    RuntimeCapability,
)

SPEAKER_STYLE = {
    1: {"color": "#DCFCE7", "text": "#166534", "side": "self-start", "badge": "green"},
    2: {"color": "#DBEAFE", "text": "#1D4ED8", "side": "self-end", "badge": "blue"},
    3: {"color": "#FFEDD5", "text": "#C2410C", "side": "self-start", "badge": "orange"},
    4: {"color": "#F3E8FF", "text": "#7E22CE", "side": "self-end", "badge": "purple"},
}


class LiveInterviewController:
    def __init__(self) -> None:
        self.state = app.storage.general
        self.service = LiveInterviewService()
        self._ensure_defaults()
        self.transcript_column: ui.column | None = None
        self.status_label: ui.label | None = None
        self.segment_badge: ui.badge | None = None
        self.recording_badge: ui.badge | None = None
        self.session_path_label: ui.label | None = None
        self.start_button: ui.button | None = None
        self.runtime_column: ui.column | None = None
        self.audio_options = self.service.list_audio_inputs()

    def _ensure_defaults(self) -> None:
        self.state.setdefault("transcription_mode", "file")
        default_audio = next(iter(self.audio_options.keys()))
        self.state.setdefault("live_audio_input", default_audio)
        self.state.setdefault("live_second_source", "None")
        self.state.setdefault("live_model", self.state.get("model") or self._default_model())
        self.state.setdefault("live_speaker_mode", "auto")
        self.state.setdefault("live_language_mode", "auto")
        self.state.setdefault("live_language", self.state.get("language"))
        self.state.setdefault("live_status", "idle")
        self.state.setdefault("live_segments", [])
        self.state.setdefault("live_notes", [])
        self.state.setdefault("live_session_path", None)
        self.state.setdefault("live_master_recording_path", None)

    def _default_model(self) -> str | None:
        options = get_model_options()
        return self.state.get("model") or (options[0] if options else None)

    def set_mode(self, mode: str) -> None:
        self.state["transcription_mode"] = mode

    def _build_config(self) -> LiveSessionConfig:
        return LiveSessionConfig(
            audio_input=self.state.get("live_audio_input", "Default microphone"),
            second_source=self.state.get("live_second_source", "None"),
            model=self.state.get("live_model"),
            speaker_mode=self.state.get("live_speaker_mode", "auto"),
            language_mode=self.state.get("live_language_mode", "auto"),
            language=self.state.get("live_language"),
            device="gpu" if self.state.get("GPU") else "cpu",
            compute_type=self.state.get("compute_type") or "int8",
            temperature=self.state.get("temperature_override"),
            initial_prompt=self.state.get("initial_prompt"),
        )

    def start_session(self) -> None:
        snapshot = self.service.start_session(self._build_config())
        self._sync_snapshot(snapshot)

    def pause_session(self) -> None:
        snapshot = self.service.pause_session()
        if snapshot is not None:
            self._sync_snapshot(snapshot)

    def finalize_session(self) -> None:
        snapshot = self.service.finalize_session()
        if snapshot is not None:
            self._sync_snapshot(snapshot)

    def _sync_snapshot(self, snapshot: LiveSessionSnapshot) -> None:
        self.state["live_status"] = snapshot.status
        self.state["live_segments"] = [asdict(segment) for segment in snapshot.segments]
        self.state["live_notes"] = snapshot.notes
        self.state["live_session_path"] = snapshot.state_path
        self.state["live_master_recording_path"] = snapshot.master_recording_path
        if self.start_button is not None:
            self.start_button.disable() if not self.service.can_record() else self.start_button.enable()
        self.refresh_status()
        self.refresh_runtime_summary()
        self.refresh_transcript()

    def refresh_from_service(self) -> None:
        if self.service.current_session is not None:
            self._sync_snapshot(self.service.current_session)

    def refresh_status(self) -> None:
        status = self.state.get("live_status", "idle")
        segments = self.state.get("live_segments", [])
        committed = len([segment for segment in segments if segment["status"] == "committed"])
        descriptions = {
            "idle": "Configure the live session, then create a local session manifest for the recorder pipeline.",
            "recording": "Session manifest created and marked recording. Attach recorder/VAD/ASR workers to publish live turns.",
            "paused": "Session paused. The manifest and transcript state remain on disk for later resume/finalization.",
            "finalized": "Session finalized. The finalizer/export worker should now generate the standard aTrain outputs.",
            "blocked": "Live mode is configured, but the required recording/transcription dependencies are not installed in this runtime.",
            "ready": "Session manifest created and waiting for the live workers to begin processing.",
        }
        if self.status_label is not None:
            self.status_label.text = descriptions.get(status, descriptions["idle"])
        if self.segment_badge is not None:
            self.segment_badge.text = f"Committed turns: {committed}"
        if self.recording_badge is not None:
            color = {
                "idle": "grey",
                "ready": "primary",
                "recording": "positive",
                "paused": "warning",
                "finalized": "dark",
                "blocked": "negative",
            }.get(status, "grey")
            self.recording_badge.text = status.capitalize()
            self.recording_badge.props(f"color={color}")
        if self.session_path_label is not None:
            state_path = self.state.get("live_session_path") or "No session manifest created yet."
            self.session_path_label.text = state_path

    def refresh_runtime_summary(self) -> None:
        if self.runtime_column is None:
            return
        self.runtime_column.clear()
        with self.runtime_column:
            for capability in self.service.capabilities:
                self._capability_row(capability)

    def refresh_transcript(self) -> None:
        if self.transcript_column is None:
            return
        self.transcript_column.clear()
        with self.transcript_column:
            segments = self.state.get("live_segments", [])
            if not segments:
                with ui.card().classes("w-full border border-dashed border-gray-300 bg-white text-gray-600"):
                    ui.label("No committed live turns yet.").classes("font-medium")
                    ui.label(
                        "Once the recorder, segmenter, ASR, and speaker workers are connected, stable turns should appear here as chat bubbles."
                    ).classes("text-sm")
            else:
                for segment in segments:
                    self._segment_card(segment)

    def add_controls(self) -> None:
        with ui.row().classes("w-full items-center justify-between gap-4"):
            with ui.row().classes("items-center gap-2"):
                self.recording_badge = ui.badge(self.state.get("live_status", "idle").capitalize())
                self.segment_badge = ui.badge("Committed turns: 0").props("outline")
            with ui.row().classes("gap-2"):
                self.start_button = ui.button(
                    "Start recording",
                    on_click=self.start_session,
                    color="dark",
                ).props("unelevated no-caps icon=radio_button_checked")
                ui.button("Pause", on_click=self.pause_session, color="gray-100").props(
                    "unelevated no-caps icon=pause text-color=dark"
                )
                ui.button(
                    "Stop and finalize",
                    on_click=self.finalize_session,
                    color="gray-100",
                ).props("unelevated no-caps icon=stop text-color=dark")
        if self.start_button is not None:
            self.start_button.disable() if not self.service.can_record() else self.start_button.enable()
        self.refresh_status()

    def render_transcript(self) -> None:
        with ui.column().classes("w-full gap-4") as transcript_column:
            self.transcript_column = transcript_column
        self.refresh_transcript()

    def _segment_card(self, segment: dict) -> None:
        speaker_id = segment.get("speaker_id_provisional") or 1
        style = SPEAKER_STYLE.get(speaker_id, SPEAKER_STYLE[1])
        bubble_classes = (
            f"w-full max-w-3xl px-4 py-3 rounded-2xl shadow-sm {segment.get('ui_side', style['side'])} "
            "border border-gray-200"
        )
        with ui.column().classes(f"w-full {segment.get('ui_side', style['side'])}"):
            with ui.card().classes(bubble_classes).style(
                f"background:{segment.get('ui_color', style['color'])}; color:{style['text']};"
            ):
                with ui.row().classes("w-full items-center justify-between gap-3"):
                    ui.badge(f"Speaker {speaker_id}").props(f"color={style['badge']}")
                    start = _format_seconds(segment.get("audio_start", 0.0))
                    end = _format_seconds(segment.get("audio_end", 0.0))
                    ui.label(f"{start} – {end}").classes("text-xs opacity-70")
                text = segment.get("final_text") or segment.get("draft_text") or ""
                ui.label(text).classes("text-sm leading-6")
                if segment.get("status") == "transcribing":
                    with ui.row().classes("items-center gap-2 text-xs opacity-70"):
                        ui.spinner(size="sm")
                        ui.label("Transcribing draft… publish once stable")

    def _capability_row(self, capability: RuntimeCapability) -> None:
        icon = "check_circle" if capability.available else "error"
        color = "positive" if capability.available else "negative"
        with ui.row().classes("w-full items-center justify-between gap-3"):
            with ui.row().classes("items-center gap-2"):
                ui.icon(icon, color=color)
                ui.label(capability.name).classes("font-medium")
            ui.label(capability.detail).classes("text-xs text-gray-600 text-right")


def input_transcription_mode(controller: LiveInterviewController) -> None:
    with ui.column().classes("w-full gap-2"):
        ui.label("Workflow Mode").classes("font-bold text-dark text-md")
        ui.separator()
        toggle = ui.toggle(
            {"file": "File Transcription", "live": "Live Interview Mode"},
            value=controller.state.get("transcription_mode", "file"),
        )
        toggle.props("unelevated toggle-color=dark no-caps")
        toggle.classes("w-full")
        toggle.bind_value(controller.state, "transcription_mode")
        toggle.on_value_change(lambda e: controller.set_mode(e.value))


def live_interview_mode(controller: LiveInterviewController) -> None:
    with ui.column().classes("w-full gap-6") as live_container:
        live_container.bind_visibility_from(
            controller.state, "transcription_mode", lambda mode: mode == "live"
        )
        with ui.element("div").classes(
            "w-full grid grid-cols-1 xl:grid-cols-[minmax(320px,420px)_1fr] gap-6"
        ):
            with ui.card().classes("p-6 gap-5 bg-gray-50"):
                ui.label("Live Interview Setup").classes("text-lg font-bold text-dark")
                ui.label(
                    "Capture from the selected microphone, segment speech live, transcribe incrementally, and run the normal aTrain export flow when finalized."
                ).classes("text-sm text-gray-600")
                _live_select(
                    "Audio input",
                    "live_audio_input",
                    controller.audio_options,
                    tooltip="Primary recording source for the local master track.",
                )
                _live_select(
                    "Second source",
                    "live_second_source",
                    {"None": "None"} | controller.audio_options,
                    tooltip="Optional second capture path when dual-source routing is available.",
                )
                _live_select(
                    "Model preset",
                    "live_model",
                    get_model_options(),
                    tooltip="Reuse the existing faster-whisper model catalog for live sessions.",
                )
                _live_select(
                    "Speaker mode",
                    "live_speaker_mode",
                    {
                        "auto": "Auto detect",
                        "two": "Force 2 speakers",
                        "four": "Allow up to 4 speakers",
                    },
                    tooltip="Use provisional labels live, then refine diarization after stop/finalize.",
                )
                with ui.column().classes("gap-2"):
                    ui.label("Language").classes("font-bold text-dark")
                    ui.separator()
                    language_mode = ui.toggle(
                        {"auto": "Auto", "fixed": "Fixed language"},
                        value=controller.state.get("live_language_mode", "auto"),
                    )
                    language_mode.props("unelevated toggle-color=dark no-caps")
                    language_mode.bind_value(controller.state, "live_language_mode")
                    language_select = ui.select(
                        get_language_options(), value=controller.state.get("live_language")
                    )
                    language_select.classes("w-full")
                    language_select.props("filled bg-color=white color=dark")
                    language_select.bind_value(controller.state, "live_language")
                    language_select.bind_visibility_from(
                        controller.state,
                        "live_language_mode",
                        lambda value: value == "fixed",
                    )
                controller.add_controls()
                with ui.column().classes("gap-2"):
                    ui.label("Runtime dependencies").classes("font-bold text-dark")
                    ui.separator()
                    with ui.column().classes("w-full gap-2") as runtime_column:
                        controller.runtime_column = runtime_column
                    controller.refresh_runtime_summary()
                with ui.column().classes("gap-2"):
                    ui.label("Pipeline checklist").classes("font-bold text-dark")
                    ui.separator()
                    for item in [
                        "1. Recorder writes a master file and rolling local chunks.",
                        "2. VAD/segmenter closes speech-aware segments with overlap.",
                        "3. ASR worker transcribes with faster-whisper.",
                        "4. Speaker worker keeps provisional speaker labels stable.",
                        "5. Finalizer reruns stronger diarization and standard exports.",
                    ]:
                        ui.label(item).classes("text-sm text-gray-700")
            with ui.column().classes("gap-4"):
                with ui.card().classes("p-6 gap-3"):
                    with ui.row().classes("w-full items-center justify-between"):
                        ui.label("Conversation View").classes("text-lg font-bold text-dark")
                        ui.switch("Auto-scroll", value=True).props("color=dark")
                    ui.label(
                        "Use a two-state transcript: a subtle draft while processing, then a committed bubble once the turn is stable."
                    ).classes("text-sm text-gray-600")
                    controller.status_label = ui.label().classes("text-sm text-gray-700")
                    controller.session_path_label = ui.label().classes(
                        "text-xs text-gray-500 break-all"
                    )
                    controller.refresh_status()
                with ui.card().classes("p-4 gap-4 min-h-[28rem] bg-slate-50"):
                    with ui.row().classes("w-full items-center justify-between"):
                        ui.input(placeholder="Search transcript").props(
                            "filled dense clearable bg-color=white"
                        )
                        ui.button("Jump to recording", color="gray-100").props(
                            "unelevated no-caps text-color=dark icon=schedule"
                        )
                    controller.render_transcript()
                with ui.card().classes("p-6 gap-3"):
                    ui.label("Recorded session assets").classes("text-base font-bold text-dark")
                    ui.label(
                        "The live session manifest is stored immediately so the recorder, ASR, and export workers can update it without changing the normal aTrain output contract."
                    ).classes("text-sm text-gray-700")
                    ui.label().bind_text_from(
                        controller.state,
                        "live_master_recording_path",
                        lambda value: value or "Master recording path will appear after session start.",
                    ).classes("text-xs text-gray-500 break-all")
                    ui.label().bind_text_from(
                        controller.state,
                        "live_notes",
                        lambda value: "\n".join(value[-5:]) if value else "Live worker notes will appear here.",
                    ).classes("text-xs text-gray-500 whitespace-pre-line")

        ui.timer(0.5, controller.refresh_from_service)


def _live_select(label: str, key: str, options: list[str] | dict[str, str], tooltip: str) -> None:
    with ui.column().classes("gap-2"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label(label).classes("font-bold text-dark")
            ui.icon("info_outline", size="sm", color="grey").tooltip(tooltip)
        ui.separator()
        select = ui.select(options=options, value=app.storage.general.get(key))
        select.classes("w-full")
        select.props("filled bg-color=white color=dark")
        select.bind_value(app.storage.general, key)


def _format_seconds(value: float) -> str:
    return str(timedelta(seconds=int(value))).zfill(8)
