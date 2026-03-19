from __future__ import annotations

from dataclasses import asdict, dataclass
import subprocess
import sys
from pathlib import Path
from typing import Literal

from nicegui import app, ui

from aTrain.components.settings.language import get_language_options
from aTrain.components.settings.model import get_model_options
from aTrain.utils.live_interview import LiveInterviewService, LiveSessionConfig

ModeName = Literal["file", "live"]
SegmentStatus = Literal["queued", "transcribing", "committed", "corrected"]

SPEAKER_STYLE = {
    1: {"color": "#DCFCE7", "text": "#166534", "side": "self-start", "badge": "green"},
    2: {"color": "#DBEAFE", "text": "#1D4ED8", "side": "self-end", "badge": "blue"},
    3: {"color": "#FFEDD5", "text": "#C2410C", "side": "self-start", "badge": "orange"},
    4: {"color": "#F3E8FF", "text": "#7E22CE", "side": "self-end", "badge": "purple"},
}

live_service = LiveInterviewService()


@dataclass
class LiveSegment:
    segment_id: str
    audio_start: str
    audio_end: str
    draft_text: str
    final_text: str
    speaker_id_provisional: int | None
    speaker_id_final: int | None
    status: SegmentStatus
    ui_side: str
    ui_color: str


class LiveInterviewController:
    def __init__(self) -> None:
        self.state = app.storage.general
        self._ensure_defaults()
        self.transcript_column: ui.column | None = None
        self.status_label: ui.label | None = None
        self.segment_badge: ui.badge | None = None
        self.recording_badge: ui.badge | None = None
        self.notes_column: ui.column | None = None
        self.runtime_column: ui.column | None = None
        self.audio_input_select: ui.select | None = None
        self.second_source_select: ui.select | None = None
        self.search_input: ui.input | None = None
        self.open_recording_button: ui.button | None = None
        self.open_session_button: ui.button | None = None
        self._last_rendered_signature: tuple | None = None
        self._last_notes_signature: tuple | None = None
        self._last_runtime_signature: tuple | None = None

    def _ensure_defaults(self) -> None:
        self.state.setdefault("transcription_mode", "file")
        self.state.setdefault("live_audio_input", "default")
        self.state.setdefault("live_second_source", "none")
        self.state.setdefault("live_model", self.state.get("model") or self._default_model())
        self.state.setdefault("live_speaker_mode", "auto")
        self.state.setdefault("live_language_mode", "auto")
        self.state.setdefault("live_language", self.state.get("language"))
        self.state.setdefault("live_status", "idle")
        self.state.setdefault("live_segments", [])
        self.state.setdefault("live_notes", [])
        self.state.setdefault("live_session_id", None)
        self.state.setdefault("live_session_dir", None)
        self.state.setdefault("live_master_recording_path", None)
        self.state.setdefault("live_search", "")

    def _default_model(self) -> str | None:
        options = get_model_options()
        if options:
            return self.state.get("model") or options[0]
        return None

    def set_mode(self, mode: ModeName) -> None:
        self.state["transcription_mode"] = mode

    def set_live_status(self, status: str) -> None:
        self.state["live_status"] = status
        self.refresh_status()

    def sync_runtime(self) -> None:
        options = live_service.list_audio_inputs()
        if self.audio_input_select is not None:
            current = self.state.get("live_audio_input")
            if current not in options:
                current = "default"
                self.state["live_audio_input"] = current
            self.audio_input_select.set_options(options, value=current)
        if self.second_source_select is not None:
            second_options = {"none": "None", **options}
            current = self.state.get("live_second_source")
            if current not in second_options:
                current = "none"
                self.state["live_second_source"] = current
            self.second_source_select.set_options(second_options, value=current)
        self.refresh_runtime_badges()

    def sync_from_service(self) -> None:
        snapshot = live_service.get_current_snapshot()
        if snapshot is None:
            if self.state.get("live_status") != "idle":
                self.state["live_status"] = "idle"
                self.state["live_segments"] = []
                self.state["live_notes"] = []
                self.state["live_session_id"] = None
                self.state["live_session_dir"] = None
                self.state["live_master_recording_path"] = None
                self.refresh_status()
                self.refresh_transcript()
                self.refresh_notes()
            return

        self.state["live_status"] = snapshot["status"]
        self.state["live_segments"] = [self._segment_to_ui(segment) for segment in snapshot["segments"]]
        self.state["live_notes"] = snapshot["notes"]
        self.state["live_session_id"] = snapshot["session_id"]
        self.state["live_master_recording_path"] = snapshot["master_recording_path"]
        self.state["live_session_dir"] = str(Path(snapshot["state_path"]).parent)
        self.refresh_status()
        self.refresh_transcript()
        self.refresh_notes()

    def start_or_resume(self) -> None:
        snapshot = live_service.start_session(self._config())
        self.sync_from_service()
        if snapshot.status == "blocked":
            ui.notify(snapshot.notes[-1] if snapshot.notes else "Live interview mode is blocked.", color="negative")
        elif snapshot.status == "recording":
            ui.notify("Live interview recording started.", color="positive")

    def pause(self) -> None:
        snapshot = live_service.pause_session()
        self.sync_from_service()
        if snapshot is None:
            ui.notify("There is no active live session to pause.", color="warning")
        else:
            ui.notify("Live interview session paused.", color="warning")

    def finalize(self) -> None:
        snapshot = live_service.finalize_session()
        self.sync_from_service()
        if snapshot is None:
            ui.notify("There is no active live session to finalize.", color="warning")
        else:
            ui.notify("Live interview session finalized. Export continues in the background.", color="positive")

    def open_recording_directory(self) -> None:
        recording_path = self.state.get("live_master_recording_path")
        if not recording_path:
            ui.notify("No live recording has been created yet.", color="warning")
            return
        directory = str(Path(recording_path).parent)
        _open_directory(directory)

    def open_session_directory(self) -> None:
        session_dir = self.state.get("live_session_dir")
        if not session_dir:
            ui.notify("No live session folder is available yet.", color="warning")
            return
        _open_directory(session_dir)

    def refresh_status(self) -> None:
        status = self.state.get("live_status", "idle")
        segments = self.state.get("live_segments", [])
        committed = len([segment for segment in segments if segment["status"] == "committed"])
        session_id = self.state.get("live_session_id")
        if self.status_label is not None:
            descriptions = {
                "idle": "Recorder idle. Configure inputs, then start a local-only session.",
                "ready": "Runtime ready. Press start to begin capturing local audio.",
                "recording": "Recording locally, buffering speech-aware chunks, and publishing committed turns.",
                "paused": "Session paused. Master recording is preserved and the transcript stays available.",
                "finalized": "Session finalized. The archival export pass is running in the background.",
                "blocked": "Live capture is blocked in the current runtime. Review the notes below for the missing dependency or device issue.",
            }
            label = descriptions.get(status, descriptions["idle"])
            if session_id:
                label = f"{label} Session: {session_id}"
            self.status_label.text = label
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
        if self.open_recording_button is not None:
            disable = "disable" if not self.state.get("live_master_recording_path") else ""
            self.open_recording_button.props(f"unelevated no-caps text-color=dark icon=folder {disable}".strip())
        if self.open_session_button is not None:
            disable = "disable" if not self.state.get("live_session_dir") else ""
            self.open_session_button.props(f"unelevated no-caps text-color=dark icon=folder_open {disable}".strip())

    def add_controls(self) -> None:
        with ui.row().classes("w-full items-center justify-between gap-4"):
            with ui.row().classes("items-center gap-2"):
                self.recording_badge = ui.badge(self.state.get("live_status", "idle").capitalize())
                self.segment_badge = ui.badge("Committed turns: 0").props("outline")
            with ui.row().classes("gap-2"):
                ui.button("Start / Resume", on_click=self.start_or_resume, color="dark").props(
                    "unelevated no-caps icon=radio_button_checked"
                )
                ui.button("Pause", on_click=self.pause, color="gray-100").props(
                    "unelevated no-caps icon=pause text-color=dark"
                )
                ui.button("Stop and finalize", on_click=self.finalize, color="gray-100").props(
                    "unelevated no-caps icon=stop text-color=dark"
                )
        self.refresh_status()

    def render_transcript(self) -> None:
        with ui.column().classes("w-full gap-4") as transcript_column:
            self.transcript_column = transcript_column
            self._render_transcript_items()

    def refresh_transcript(self) -> None:
        signature = (
            self.state.get("live_search", "").strip().lower(),
            tuple(
                (
                    segment["segment_id"],
                    segment["status"],
                    segment["audio_start"],
                    segment["audio_end"],
                    segment["draft_text"],
                    segment["final_text"],
                    segment["speaker_id_provisional"],
                )
                for segment in self.state.get("live_segments", [])
            ),
        )
        if signature == self._last_rendered_signature:
            return
        self._last_rendered_signature = signature
        if self.transcript_column is not None:
            self._render_transcript_items()

    def _render_transcript_items(self) -> None:
        assert self.transcript_column is not None
        query = self.state.get("live_search", "").strip().lower()
        self.transcript_column.clear()
        with self.transcript_column:
            segments = self.state.get("live_segments", [])
            visible_segments = [segment for segment in segments if self._matches_search(segment, query)]
            if not visible_segments:
                ui.label(
                    "No live transcript turns yet." if not query else "No transcript turns matched your search."
                ).classes("text-sm text-gray-500")
            for segment in visible_segments:
                self._segment_card(segment)

    def _segment_card(self, segment: dict) -> None:
        speaker_id = segment.get("speaker_id_provisional") or 1
        style = SPEAKER_STYLE.get(speaker_id, SPEAKER_STYLE[1])
        bubble_classes = (
            f"w-full max-w-3xl px-4 py-3 rounded-2xl shadow-sm {segment['ui_side']} "
            "border border-gray-200"
        )
        with ui.column().classes(f"w-full {segment['ui_side']}"):
            with ui.card().classes(bubble_classes).style(
                f"background:{segment.get('ui_color') or style['color']}; color:{style['text']};"
            ):
                with ui.row().classes("w-full items-center justify-between gap-3"):
                    ui.badge(f"Speaker {speaker_id}").props(f"color={style['badge']}")
                    ui.label(f"{segment['audio_start']} – {segment['audio_end']}").classes("text-xs opacity-70")
                text = segment["final_text"] if segment["status"] != "transcribing" else segment["draft_text"]
                ui.label(text or "Listening…").classes("text-sm leading-6")
                if segment["status"] == "transcribing":
                    with ui.row().classes("items-center gap-2 text-xs opacity-70"):
                        ui.spinner(size="sm")
                        ui.label("Transcribing draft… publish once stable")

    def render_runtime_notes(self) -> None:
        with ui.column().classes("gap-2"):
            ui.label("Runtime").classes("font-bold text-dark")
            ui.separator()
            self.runtime_column = ui.column().classes("gap-2")
            self.refresh_runtime_badges()

    def refresh_runtime_badges(self) -> None:
        signature = tuple((cap.name, cap.available, cap.detail) for cap in live_service.capabilities)
        if signature == self._last_runtime_signature or self.runtime_column is None:
            return
        self._last_runtime_signature = signature
        self.runtime_column.clear()
        with self.runtime_column:
            for capability in live_service.capabilities:
                color = "positive" if capability.available else "negative"
                with ui.row().classes("items-center justify-between gap-4 w-full"):
                    ui.label(capability.name).classes("text-sm text-dark")
                    ui.badge("Available" if capability.available else "Missing").props(f"color={color}")
                ui.label(capability.detail).classes("text-xs text-gray-500")

    def render_notes(self) -> None:
        with ui.column().classes("gap-2"):
            ui.label("Session Notes").classes("font-bold text-dark")
            ui.separator()
            self.notes_column = ui.column().classes("gap-2 max-h-52 overflow-auto")
            self.refresh_notes()

    def refresh_notes(self) -> None:
        notes = tuple(self.state.get("live_notes", []))
        if notes == self._last_notes_signature or self.notes_column is None:
            return
        self._last_notes_signature = notes
        self.notes_column.clear()
        with self.notes_column:
            if not notes:
                ui.label("No live session notes yet.").classes("text-sm text-gray-500")
            for note in reversed(notes):
                ui.label(note).classes("text-xs text-gray-700 break-words")

    def _matches_search(self, segment: dict, query: str) -> bool:
        if not query:
            return True
        haystacks = [
            segment.get("draft_text", ""),
            segment.get("final_text", ""),
            segment.get("segment_id", ""),
            f"speaker {segment.get('speaker_id_provisional') or ''}",
        ]
        return any(query in value.lower() for value in haystacks)

    def _config(self) -> LiveSessionConfig:
        return LiveSessionConfig(
            audio_input=self.state.get("live_audio_input") or "default",
            second_source=self.state.get("live_second_source") or "none",
            model=self.state.get("live_model"),
            speaker_mode=self.state.get("live_speaker_mode") or "auto",
            language_mode=self.state.get("live_language_mode") or "auto",
            language=self.state.get("live_language"),
            device="gpu" if self.state.get("GPU") else "cpu",
            compute_type=self.state.get("compute_type") or "int8",
            temperature=self.state.get("temperature_override"),
            initial_prompt=self.state.get("initial_prompt") or None,
        )

    def _segment_to_ui(self, segment: dict) -> dict:
        speaker_id = segment.get("speaker_id_provisional") or segment.get("speaker_id_final") or 1
        style = SPEAKER_STYLE.get(speaker_id, SPEAKER_STYLE[1])
        text = segment.get("final_text") or ""
        draft = segment.get("draft_text") or (
            "Listening for a stable turn…" if segment.get("status") == "transcribing" else ""
        )
        ui_segment = LiveSegment(
            segment_id=segment["segment_id"],
            audio_start=_format_seconds(segment.get("audio_start", 0.0)),
            audio_end=_format_seconds(segment.get("audio_end", 0.0)),
            draft_text=draft,
            final_text=text,
            speaker_id_provisional=segment.get("speaker_id_provisional"),
            speaker_id_final=segment.get("speaker_id_final"),
            status=segment.get("status", "queued"),
            ui_side=segment.get("ui_side") or style["side"],
            ui_color=segment.get("ui_color") or style["color"],
        )
        return asdict(ui_segment)


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


def live_interview_mode() -> None:
    controller = LiveInterviewController()
    with ui.column().classes("w-full gap-6") as live_container:
        live_container.bind_visibility_from(controller.state, "transcription_mode", lambda mode: mode == "live")
        with ui.element("div").classes("w-full grid grid-cols-1 xl:grid-cols-[minmax(320px,420px)_1fr] gap-6"):
            with ui.card().classes("p-6 gap-5 bg-gray-50"):
                ui.label("Live Interview Setup").classes("text-lg font-bold text-dark")
                ui.label(
                    "Capture locally, transcribe incrementally in the background, and keep the normal aTrain export files unchanged."
                ).classes("text-sm text-gray-600")
                controller.audio_input_select = _live_select(
                    "Audio input",
                    "live_audio_input",
                    {"default": "Default system microphone"},
                    tooltip="Primary recording source for the local master track.",
                )
                controller.second_source_select = _live_select(
                    "Second source",
                    "live_second_source",
                    {"none": "None", "default": "Default system microphone"},
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
                        controller.state, "live_language_mode", lambda value: value == "fixed"
                    )
                controller.add_controls()
                with ui.row().classes("gap-2"):
                    controller.open_recording_button = ui.button(
                        "Open recording folder",
                        on_click=controller.open_recording_directory,
                        color="gray-100",
                    )
                    controller.open_session_button = ui.button(
                        "Open session folder",
                        on_click=controller.open_session_directory,
                        color="gray-100",
                    )
                controller.refresh_status()
                controller.render_runtime_notes()
                controller.render_notes()
            with ui.column().classes("gap-4"):
                with ui.card().classes("p-6 gap-3"):
                    with ui.row().classes("w-full items-center justify-between"):
                        ui.label("Conversation View").classes("text-lg font-bold text-dark")
                        ui.switch("Auto-refresh", value=True).props("color=dark disable")
                    ui.label(
                        "Live turns appear first as transcribing draft segments and switch to committed bubbles once the worker finishes."
                    ).classes("text-sm text-gray-600")
                    controller.status_label = ui.label().classes("text-sm text-gray-700")
                    controller.refresh_status()
                with ui.card().classes("p-4 gap-4 min-h-[28rem] bg-slate-50"):
                    with ui.row().classes("w-full items-center justify-between gap-3"):
                        controller.search_input = ui.input(placeholder="Search transcript")
                        controller.search_input.props("filled dense clearable bg-color=white").classes("grow")
                        controller.search_input.bind_value(controller.state, "live_search")
                        controller.search_input.on_value_change(lambda _: controller.refresh_transcript())
                        ui.button(
                            "Refresh now",
                            color="gray-100",
                            on_click=lambda: (controller.sync_runtime(), controller.sync_from_service()),
                        ).props("unelevated no-caps text-color=dark icon=refresh")
                    controller.render_transcript()
                with ui.card().classes("p-6 gap-3"):
                    ui.label("Export compatibility").classes("text-base font-bold text-dark")
                    ui.label(
                        "Stopping a live session preserves the master recording and launches the standard final export pass in the background."
                    ).classes("text-sm text-gray-700")
        ui.timer(0.5, lambda: (controller.sync_runtime(), controller.sync_from_service()))


def _live_select(label: str, key: str, options: list[str] | dict[str, str], tooltip: str) -> ui.select:
    with ui.column().classes("gap-2"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label(label).classes("font-bold text-dark")
            ui.icon("info_outline", size="sm", color="grey").tooltip(tooltip)
        ui.separator()
        select = ui.select(options=options, value=app.storage.general.get(key))
        select.classes("w-full")
        select.props("filled bg-color=white color=dark")
        select.bind_value(app.storage.general, key)
    return select


def _format_seconds(value: float | int | None) -> str:
    if value is None:
        value = 0
    total_seconds = max(0, int(round(float(value))))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _open_directory(directory: str) -> None:
    if sys.platform.startswith("linux"):
        subprocess.run(["xdg-open", directory], check=False)
        return
    from showinfm import show_in_file_manager

    show_in_file_manager(directory)
