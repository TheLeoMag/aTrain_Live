from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from aTrain_core.globals import REQUIRED_MODELS
from nicegui import app, ui

from aTrain.components.settings.language import get_language_options
from aTrain.components.settings.model import get_model_options

ModeName = Literal["file", "live"]
SegmentStatus = Literal["queued", "transcribing", "committed", "corrected"]

SPEAKER_STYLE = {
    1: {"color": "#DCFCE7", "text": "#166534", "side": "self-start", "badge": "green"},
    2: {"color": "#DBEAFE", "text": "#1D4ED8", "side": "self-end", "badge": "blue"},
    3: {"color": "#FFEDD5", "text": "#C2410C", "side": "self-start", "badge": "orange"},
    4: {"color": "#F3E8FF", "text": "#7E22CE", "side": "self-end", "badge": "purple"},
}


@dataclass
class LiveSegment:
    segment_id: str
    audio_start: str
    audio_end: str
    draft_text: str
    final_text: str
    speaker_id_provisional: int
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

    def _ensure_defaults(self) -> None:
        self.state.setdefault("transcription_mode", "file")
        self.state.setdefault("live_audio_input", "Default microphone")
        self.state.setdefault("live_second_source", "None")
        self.state.setdefault("live_model", self.state.get("model") or self._default_model())
        self.state.setdefault("live_speaker_mode", "auto")
        self.state.setdefault("live_language_mode", "auto")
        self.state.setdefault("live_language", self.state.get("language"))
        self.state.setdefault("live_status", "idle")
        self.state.setdefault("live_segments", self._seed_segments())

    def _default_model(self) -> str | None:
        options = get_model_options()
        if options:
            return self.state.get("model") or options[0]
        return REQUIRED_MODELS[1] if len(REQUIRED_MODELS) > 1 else None

    def _seed_segments(self) -> list[dict]:
        seeded = [
            LiveSegment(
                segment_id="seg-001",
                audio_start="00:00:14",
                audio_end="00:00:22",
                draft_text="",
                final_text="Thanks for joining. Could you briefly describe the onboarding process?",
                speaker_id_provisional=1,
                speaker_id_final=None,
                status="committed",
                ui_side=SPEAKER_STYLE[1]["side"],
                ui_color=SPEAKER_STYLE[1]["color"],
            ),
            LiveSegment(
                segment_id="seg-002",
                audio_start="00:00:23",
                audio_end="00:00:31",
                draft_text="",
                final_text="Sure — we usually start with a shadowing week and a checklist for the first month.",
                speaker_id_provisional=2,
                speaker_id_final=None,
                status="committed",
                ui_side=SPEAKER_STYLE[2]["side"],
                ui_color=SPEAKER_STYLE[2]["color"],
            ),
            LiveSegment(
                segment_id="seg-003",
                audio_start="00:00:32",
                audio_end="00:00:38",
                draft_text="Detecting pause boundary and stabilizing text…",
                final_text="",
                speaker_id_provisional=1,
                speaker_id_final=None,
                status="transcribing",
                ui_side=SPEAKER_STYLE[1]["side"],
                ui_color=SPEAKER_STYLE[1]["color"],
            ),
        ]
        return [asdict(segment) for segment in seeded]

    def set_mode(self, mode: ModeName) -> None:
        self.state["transcription_mode"] = mode

    def set_live_status(self, status: str) -> None:
        self.state["live_status"] = status
        self.refresh_status()

    def refresh_status(self) -> None:
        status = self.state.get("live_status", "idle")
        segments = self.state.get("live_segments", [])
        committed = len([segment for segment in segments if segment["status"] == "committed"])
        if self.status_label is not None:
            descriptions = {
                "idle": "Recorder idle. Configure inputs, then start a local-only session.",
                "recording": "Recording locally, buffering speech-aware chunks, and publishing only stable turns.",
                "paused": "Session paused. Master recording is preserved and the live transcript remains editable.",
                "finalized": "Session finalized. Run the stronger diarization/export pass to keep standard aTrain outputs unchanged.",
            }
            self.status_label.text = descriptions.get(status, descriptions["idle"])
        if self.segment_badge is not None:
            self.segment_badge.text = f"Committed turns: {committed}"
        if self.recording_badge is not None:
            color = {
                "idle": "grey",
                "recording": "positive",
                "paused": "warning",
                "finalized": "dark",
            }.get(status, "grey")
            self.recording_badge.text = status.capitalize()
            self.recording_badge.props(f"color={color}")

    def add_controls(self) -> None:
        with ui.row().classes("w-full items-center justify-between gap-4"):
            with ui.row().classes("items-center gap-2"):
                self.recording_badge = ui.badge(self.state.get("live_status", "idle").capitalize())
                self.segment_badge = ui.badge("Committed turns: 0").props("outline")
            with ui.row().classes("gap-2"):
                ui.button("Start recording", on_click=lambda: self.set_live_status("recording"), color="dark").props("unelevated no-caps icon=radio_button_checked")
                ui.button("Pause", on_click=lambda: self.set_live_status("paused"), color="gray-100").props("unelevated no-caps icon=pause text-color=dark")
                ui.button("Stop and finalize", on_click=lambda: self.set_live_status("finalized"), color="gray-100").props("unelevated no-caps icon=stop text-color=dark")
        self.refresh_status()

    def render_transcript(self) -> None:
        with ui.column().classes("w-full gap-4") as transcript_column:
            self.transcript_column = transcript_column
            self._render_transcript_items()

    def _render_transcript_items(self) -> None:
        assert self.transcript_column is not None
        self.transcript_column.clear()
        with self.transcript_column:
            for segment in self.state.get("live_segments", []):
                self._segment_card(segment)

    def _segment_card(self, segment: dict) -> None:
        speaker_id = segment["speaker_id_provisional"]
        style = SPEAKER_STYLE.get(speaker_id, SPEAKER_STYLE[1])
        bubble_classes = (
            f"w-full max-w-3xl px-4 py-3 rounded-2xl shadow-sm {segment['ui_side']} "
            "border border-gray-200"
        )
        with ui.column().classes(f"w-full {segment['ui_side']}"):
            with ui.card().classes(bubble_classes).style(f"background:{style['color']}; color:{style['text']};"):
                with ui.row().classes("w-full items-center justify-between gap-3"):
                    ui.badge(f"Speaker {speaker_id}").props(f"color={style['badge']}")
                    ui.label(f"{segment['audio_start']} – {segment['audio_end']}").classes("text-xs opacity-70")
                text = segment["final_text"] if segment["status"] != "transcribing" else segment["draft_text"]
                ui.label(text).classes("text-sm leading-6")
                if segment["status"] == "transcribing":
                    with ui.row().classes("items-center gap-2 text-xs opacity-70"):
                        ui.spinner(size="sm")
                        ui.label("Transcribing draft… publish once stable")


def input_transcription_mode(controller: LiveInterviewController) -> None:
    with ui.column().classes("w-full gap-2"):
        ui.label("Workflow Mode").classes("font-bold text-dark text-md")
        ui.separator()
        toggle = ui.toggle({"file": "File Transcription", "live": "Live Interview Mode"}, value=controller.state.get("transcription_mode", "file"))
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
                ui.label("Capture locally, transcribe incrementally in the background, and keep the normal aTrain export files unchanged.").classes("text-sm text-gray-600")
                _live_select("Audio input", "live_audio_input", ["Default microphone", "USB microphone", "System loopback"], tooltip="Primary recording source for the local master track.")
                _live_select("Second source", "live_second_source", ["None", "USB microphone 2", "Virtual cable"], tooltip="Optional second capture path when dual-source routing is available.")
                _live_select("Model preset", "live_model", get_model_options(), tooltip="Reuse the existing faster-whisper model catalog for live sessions.")
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
                    language_mode = ui.toggle({"auto": "Auto", "fixed": "Fixed language"}, value=controller.state.get("live_language_mode", "auto"))
                    language_mode.props("unelevated toggle-color=dark no-caps")
                    language_mode.bind_value(controller.state, "live_language_mode")
                    language_select = ui.select(get_language_options(), value=controller.state.get("live_language"))
                    language_select.classes("w-full")
                    language_select.props("filled bg-color=white color=dark")
                    language_select.bind_value(controller.state, "live_language")
                    language_select.bind_visibility_from(controller.state, "live_language_mode", lambda value: value == "fixed")
                controller.add_controls()
                with ui.column().classes("gap-2"):
                    ui.label("Pipeline").classes("font-bold text-dark")
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
                    ui.label("Use a two-state transcript: a subtle draft while processing, then a committed bubble once the turn is stable.").classes("text-sm text-gray-600")
                    controller.status_label = ui.label().classes("text-sm text-gray-700")
                    controller.refresh_status()
                with ui.card().classes("p-4 gap-4 min-h-[28rem] bg-slate-50"):
                    with ui.row().classes("w-full items-center justify-between"):
                        ui.input(placeholder="Search transcript").props("filled dense clearable bg-color=white")
                        ui.button("Jump to recording", color="gray-100").props("unelevated no-caps text-color=dark icon=schedule")
                    controller.render_transcript()
                with ui.card().classes("p-6 gap-3"):
                    ui.label("Export compatibility").classes("text-base font-bold text-dark")
                    ui.label("Live Interview Mode keeps the archival recording, live session JSON state, chat transcript metadata, and the final standard aTrain outputs aligned with the existing export workflow.").classes("text-sm text-gray-700")


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
