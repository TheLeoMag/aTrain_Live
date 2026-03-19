from nicegui import Client, ui

from aTrain.components.settings.advanced import advanced_settings
from aTrain.components.settings.file import input_file
from aTrain.components.settings.language import input_language
from aTrain.components.settings.live_mode import (
    LiveInterviewController,
    input_transcription_mode,
    live_interview_mode,
)
from aTrain.components.settings.model import input_model
from aTrain.components.settings.speaker_count import input_speaker_count
from aTrain.components.settings.speaker_detection import input_speaker_detection
from aTrain.components.splash_screen import splash_screen
from aTrain.layouts.base import base_layout
from aTrain.utils.transcription import start_transcription


@ui.page("/")
async def page(client: Client):
    await client.connected()
    await splash_screen()
    with base_layout():
        live_controller = LiveInterviewController()
        input_transcription_mode(live_controller)
        with ui.column().classes("w-full gap-6") as file_mode:
            file_mode.bind_visibility_from(live_controller.state, "transcription_mode", lambda mode: mode == "file")
            with ui.element("div").classes(
                "w-full h-full grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-10"
            ):
                file = input_file()
                input_model()
                input_language()
                input_speaker_detection()
                input_speaker_count()
            ui.separator().classes("mt-4")
            with ui.row().classes("w-full justify-between items-center"):
                settings_btn = ui.button("Advanced Settings", color="gray-100")
                settings_btn.props("size=0.8rem unelevated no-caps icon=settings")
                start_btn = ui.button("Start", on_click=file.upload, color="dark")
                start_btn.props("no-caps unelevated")
                advanced_settings(open=False)

        live_interview_mode(live_controller)

    file.on_upload(start_transcription)
    settings_btn.on_click(lambda: advanced_settings(open=True))
