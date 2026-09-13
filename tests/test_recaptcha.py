import asyncio
import json
import os
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
import speech_recognition as sr

from map_miner.recaptcha_solver import (
    RecaptchaBlockedError,
    RecaptchaSolveError,
    RecaptchaSolver,
    normalize_audio_transcription,
    preprocess_audio,
    save_captcha_diagnostics,
    transcribe_audio,
)


def test_recaptcha_solver_init():
    mock_page = MagicMock()
    solver = RecaptchaSolver(mock_page, debug=False)
    assert solver.page == mock_page
    assert not solver.debug


def test_recaptcha_solver_aliases():
    mock_page = MagicMock()
    solver = RecaptchaSolver(mock_page)
    assert solver.solveCaptcha == solver.solve_captcha
    assert solver.solveAudioCaptcha == solver.solve_audio_captcha
    assert solver.isSolved == solver.is_solved


def test_recaptcha_blocked_detection():
    async def _run():
        mock_page = MagicMock()
        mock_frame = MagicMock()

        mock_dos_loc = MagicMock()
        mock_dos_loc.count = AsyncMock(return_value=1)

        mock_frame.locator = MagicMock(return_value=mock_dos_loc)
        mock_page.locator = MagicMock(
            return_value=MagicMock(count=AsyncMock(return_value=0))
        )

        solver = RecaptchaSolver(mock_page)
        assert await solver.is_hard_blocked(mock_frame) is True

        mock_empty_loc = MagicMock()
        mock_empty_loc.count = AsyncMock(return_value=0)
        mock_body_loc = MagicMock()
        mock_body_loc.count = AsyncMock(return_value=1)
        mock_body_loc.inner_text = AsyncMock(
            return_value="Your computer or network may be sending automated queries"
        )

        def frame_locator_side_effect(selector):
            if "body" in selector:
                return mock_body_loc
            return mock_empty_loc

        mock_frame.locator.side_effect = frame_locator_side_effect
        assert await solver.is_hard_blocked(mock_frame) is True

        # Test solve_audio_captcha raises RecaptchaBlockedError when is_hard_blocked is True
        with patch.object(solver, "is_hard_blocked", AsyncMock(return_value=True)):
            mock_page.frame_locator.return_value = mock_frame
            mock_frame.locator.return_value = MagicMock(
                count=AsyncMock(return_value=0),
                is_visible=AsyncMock(return_value=False),
            )
            with pytest.raises(RecaptchaBlockedError) as exc_info:
                await solver.solve_audio_captcha()
            assert "automated queries" in str(exc_info.value)

    asyncio.run(_run())


def test_preprocess_audio():
    with patch("map_miner.recaptcha_solver.AudioSegment") as mock_audio_segment:
        mock_sound = MagicMock()
        mock_audio_segment.from_mp3.return_value = mock_sound
        mock_sound.set_frame_rate.return_value = mock_sound
        mock_sound.set_channels.return_value = mock_sound
        mock_sound.normalize.return_value = mock_sound
        mock_sound.high_pass_filter.return_value = mock_sound
        mock_sound.low_pass_filter.return_value = mock_sound

        preprocess_audio("sample.mp3", "output.wav")

        mock_audio_segment.from_mp3.assert_called_once_with("sample.mp3")
        mock_sound.set_frame_rate.assert_called_once_with(16000)
        mock_sound.set_channels.assert_called_once_with(1)
        mock_sound.normalize.assert_called_once()
        mock_sound.high_pass_filter.assert_called_once_with(300)
        mock_sound.low_pass_filter.assert_called_once_with(3400)
        mock_sound.export.assert_called_once_with("output.wav", format="wav")


def test_transcribe_audio_fallback():
    with (
        patch("speech_recognition.AudioFile"),
        patch("speech_recognition.Recognizer") as mock_recognizer_cls,
    ):
        mock_recognizer = MagicMock()
        mock_recognizer_cls.return_value = mock_recognizer

        # Scenario 1: Whisper available and succeeds
        mock_recognizer.recognize_whisper.return_value = "HELLO, WORLD!"
        result = transcribe_audio("test.wav", engine="auto")
        assert result == "hello world"

        # Scenario 2: Whisper fails, Google succeeds
        mock_recognizer.recognize_whisper.side_effect = ImportError(
            "whisper not installed"
        )
        mock_recognizer.recognize_google.return_value = "One, Two; Three! 123"
        result = transcribe_audio("test.wav", engine="auto")
        assert result == "one two three 123"

        # Scenario 3: Whisper & Google fail, Vosk succeeds
        mock_recognizer.recognize_google.side_effect = sr.RequestError("quota exceeded")
        mock_recognizer.recognize_vosk.return_value = '{"text": "spoken words here"}'
        result = transcribe_audio("test.wav", engine="auto")
        assert result == "spoken words here"

        # Scenario 4: All engines fail
        mock_recognizer.recognize_vosk.side_effect = ImportError("vosk not installed")
        with pytest.raises(RecaptchaSolveError):
            transcribe_audio("test.wav", engine="auto")


def test_sorry_page_submission():
    async def _run():
        mock_page = MagicMock()
        mock_page.url = (
            "https://www.google.com/sorry/index?continue=https://www.google.com/maps"
        )

        mock_submit_btn = MagicMock()
        mock_submit_btn.count = AsyncMock(return_value=1)
        mock_submit_btn.first = MagicMock()
        mock_submit_btn.first.is_visible = AsyncMock(return_value=True)
        mock_submit_btn.first.click = AsyncMock()

        mock_page.locator = MagicMock(return_value=mock_submit_btn)
        mock_page.wait_for_url = AsyncMock()

        solver = RecaptchaSolver(mock_page)

        with patch.object(solver, "is_solved", AsyncMock(return_value=True)):
            solved = await solver.solve_captcha()
            assert solved is True

            mock_submit_btn.first.click.assert_awaited_once()
            mock_page.wait_for_url.assert_awaited_once()
            predicate = mock_page.wait_for_url.call_args[0][0]
            assert callable(predicate)
            assert predicate("https://www.google.com/maps") is True
            assert predicate("https://www.google.com/sorry/index") is False

    asyncio.run(_run())


def test_multi_round_audio_captcha_solving():
    async def _run():
        mock_page = MagicMock()
        mock_challenge_frame = MagicMock()
        mock_page.frame_locator.return_value = mock_challenge_frame

        mock_resp_input = MagicMock()
        mock_resp_input.is_visible = AsyncMock(return_value=False)
        mock_resp_input.fill = AsyncMock()
        mock_resp_input.press = AsyncMock()

        mock_audio_btn = MagicMock()
        mock_audio_btn.wait_for = AsyncMock()
        mock_audio_btn.click = AsyncMock()

        mock_audio_src = MagicMock()
        mock_audio_src.count = AsyncMock(return_value=1)
        mock_audio_src.is_visible = AsyncMock(return_value=False)

        src_state = {"src": "https://www.google.com/recaptcha/api2/payload?id=round1"}

        async def mock_get_attribute(attr, *args, **kwargs):
            if attr == "src":
                return src_state["src"]
            return None

        mock_audio_src.get_attribute = AsyncMock(side_effect=mock_get_attribute)

        mock_verify_btn = MagicMock()
        mock_verify_btn.is_visible = AsyncMock(return_value=True)
        mock_verify_btn.click = AsyncMock()

        mock_reload_btn = MagicMock()
        mock_reload_btn.is_visible = AsyncMock(return_value=False)

        def challenge_locator(selector):
            if "#recaptcha-audio-button" in selector:
                return mock_audio_btn
            if "#audio-response" in selector:
                return mock_resp_input
            if "#audio-source" in selector:
                return mock_audio_src
            if "#recaptcha-verify-button" in selector:
                return mock_verify_btn
            if "#recaptcha-reload-button" in selector:
                return mock_reload_btn
            return MagicMock(
                count=AsyncMock(return_value=0),
                is_visible=AsyncMock(return_value=False),
            )

        mock_challenge_frame.locator.side_effect = challenge_locator

        solver = RecaptchaSolver(mock_page)

        solved_state = {"solved": False}

        async def mock_is_solved():
            return solved_state["solved"]

        transcribe_calls = [0]

        def mock_transcribe(*args, **kwargs):
            transcribe_calls[0] += 1
            if transcribe_calls[0] == 1:
                # Round 1 finished: simulate Google updating the audio source URL for round 2
                src_state["src"] = (
                    "https://www.google.com/recaptcha/api2/payload?id=round2"
                )
                return "first code"
            # Round 2 finished: CAPTCHA is now solved
            solved_state["solved"] = True
            return "second code"

        with (
            patch.object(solver, "is_hard_blocked", AsyncMock(return_value=False)),
            patch.object(solver, "is_solved", side_effect=mock_is_solved),
            patch.object(solver, "download_audio", AsyncMock()) as mock_dl,
            patch("map_miner.recaptcha_solver.preprocess_audio") as mock_prep,
            patch(
                "map_miner.recaptcha_solver.transcribe_audio",
                side_effect=mock_transcribe,
            ),
            patch("asyncio.sleep", AsyncMock()),
        ):
            solved = await solver.solve_audio_captcha(max_audio_attempts=4)
            assert solved is True
            assert mock_dl.call_count == 2
            mock_dl.assert_any_call(
                "https://www.google.com/recaptcha/api2/payload?id=round1", ANY
            )
            mock_dl.assert_any_call(
                "https://www.google.com/recaptcha/api2/payload?id=round2", ANY
            )
            assert mock_prep.call_count == 2
            assert transcribe_calls[0] == 2
            assert (
                mock_resp_input.press_sequentially.call_count == 2
                or mock_resp_input.fill.call_count == 2
            )
            assert mock_verify_btn.click.call_count == 2

    asyncio.run(_run())


def test_multi_round_audio_captcha_reload_fallback():
    async def _run():
        mock_page = MagicMock()
        mock_challenge_frame = MagicMock()
        mock_page.frame_locator.return_value = mock_challenge_frame

        mock_resp_input = MagicMock()
        mock_resp_input.is_visible = AsyncMock(return_value=True)
        mock_resp_input.fill = AsyncMock()
        mock_resp_input.press = AsyncMock()

        mock_audio_src = MagicMock()
        mock_audio_src.count = AsyncMock(return_value=1)
        mock_audio_src.is_visible = AsyncMock(return_value=True)

        # Initially no src or empty, until reload clicked
        src_state = {"src": ""}

        async def mock_get_attribute(attr, *args, **kwargs):
            if attr == "src":
                return src_state["src"]
            return None

        mock_audio_src.get_attribute = AsyncMock(side_effect=mock_get_attribute)

        mock_body = MagicMock()
        mock_body.inner_text = AsyncMock(return_value="Please try again.")

        mock_reload_btn = MagicMock()
        mock_reload_btn.is_visible = AsyncMock(return_value=True)

        async def on_reload():
            src_state["src"] = (
                "https://www.google.com/recaptcha/api2/payload?id=reloaded"
            )

        mock_reload_btn.click = AsyncMock(side_effect=on_reload)

        mock_verify_btn = MagicMock()
        mock_verify_btn.is_visible = AsyncMock(return_value=True)
        mock_verify_btn.click = AsyncMock()

        def challenge_locator(selector):
            if "#audio-response" in selector:
                return mock_resp_input
            if "#audio-source" in selector:
                return mock_audio_src
            if "body" in selector:
                return mock_body
            if "#recaptcha-reload-button" in selector:
                return mock_reload_btn
            if "#recaptcha-verify-button" in selector:
                return mock_verify_btn
            return MagicMock(
                count=AsyncMock(return_value=0),
                is_visible=AsyncMock(return_value=False),
            )

        mock_challenge_frame.locator.side_effect = challenge_locator

        solver = RecaptchaSolver(mock_page)
        solved_state = {"solved": False}

        async def mock_is_solved():
            return solved_state["solved"]

        def mock_transcribe(*args, **kwargs):
            solved_state["solved"] = True
            return "reloaded code"

        fake_time = 0.0

        def fake_monotonic():
            nonlocal fake_time
            return fake_time

        async def fake_sleep(duration):
            nonlocal fake_time
            fake_time += duration

        with (
            patch("time.monotonic", side_effect=fake_monotonic),
            patch("asyncio.sleep", side_effect=fake_sleep),
            patch.object(solver, "is_hard_blocked", AsyncMock(return_value=False)),
            patch.object(solver, "is_solved", side_effect=mock_is_solved),
            patch.object(solver, "download_audio", AsyncMock()) as mock_dl,
            patch("map_miner.recaptcha_solver.preprocess_audio"),
            patch(
                "map_miner.recaptcha_solver.transcribe_audio",
                side_effect=mock_transcribe,
            ),
        ):
            solved = await solver.solve_audio_captcha(max_audio_attempts=4)
            assert solved is True
            mock_reload_btn.click.assert_awaited()
            mock_dl.assert_called_with(
                "https://www.google.com/recaptcha/api2/payload?id=reloaded", ANY
            )

    asyncio.run(_run())


def test_audio_source_missing_does_not_call_get_attribute():
    """Verifies that if #audio-source is missing (count == 0), get_attribute is NOT called,
    and polling exits via monotonic timeout without hanging.
    """

    async def _run():
        mock_page = MagicMock()
        mock_challenge_frame = MagicMock()
        mock_page.frame_locator.return_value = mock_challenge_frame

        mock_resp_input = MagicMock()
        mock_resp_input.is_visible = AsyncMock(return_value=True)

        mock_audio_src = MagicMock()
        mock_audio_src.count = AsyncMock(return_value=0)
        mock_audio_src.get_attribute = AsyncMock()

        def challenge_locator(selector):
            if "#audio-response" in selector:
                return mock_resp_input
            if "#audio-source" in selector:
                return mock_audio_src
            return MagicMock(
                count=AsyncMock(return_value=0),
                is_visible=AsyncMock(return_value=False),
            )

        mock_challenge_frame.locator.side_effect = challenge_locator
        solver = RecaptchaSolver(mock_page)

        fake_time = 0.0

        def fake_monotonic():
            nonlocal fake_time
            return fake_time

        async def fake_sleep(duration):
            nonlocal fake_time
            fake_time += duration

        with (
            patch("time.monotonic", side_effect=fake_monotonic),
            patch("asyncio.sleep", side_effect=fake_sleep),
            patch.object(solver, "is_hard_blocked", AsyncMock(return_value=False)),
            patch.object(solver, "is_solved", AsyncMock(return_value=False)),
        ):
            with pytest.raises(RecaptchaSolveError) as exc_info:
                await solver.solve_audio_captcha(max_audio_attempts=1)
            assert "Audio challenge source URL is missing" in str(exc_info.value)
            mock_audio_src.get_attribute.assert_not_called()
            assert fake_time >= 8.0

    asyncio.run(_run())


def test_is_hard_blocked_uses_timeout():
    """Verifies that is_hard_blocked calls inner_text with timeout=1000."""

    async def _run():
        mock_page = MagicMock()
        mock_challenge_frame = MagicMock()

        mock_dos_loc = MagicMock()
        mock_dos_loc.count = AsyncMock(return_value=0)

        mock_body_loc = MagicMock()
        mock_body_loc.count = AsyncMock(return_value=1)
        mock_body_loc.inner_text = AsyncMock(return_value="try again later")

        def challenge_locator(selector):
            if selector == "body":
                return mock_body_loc
            return mock_dos_loc

        mock_challenge_frame.locator.side_effect = challenge_locator
        solver = RecaptchaSolver(mock_page)
        blocked = await solver.is_hard_blocked(mock_challenge_frame)
        assert blocked is True
        mock_body_loc.inner_text.assert_awaited_once_with(timeout=1000)

    asyncio.run(_run())


def test_is_solved_uses_timeout():
    """Verifies that is_solved calls get_attribute with timeout=1000."""

    async def _run():
        mock_page = MagicMock()
        mock_frame = MagicMock()
        mock_page.frame_locator.return_value = mock_frame

        mock_checkbox = MagicMock()
        mock_checkbox.count = AsyncMock(return_value=1)
        mock_checkbox.get_attribute = AsyncMock(
            side_effect=lambda attr, **kw: "true" if attr == "aria-checked" else ""
        )
        mock_frame.locator.return_value = mock_checkbox

        solver = RecaptchaSolver(mock_page)
        solved = await solver.is_solved()
        assert solved is True
        mock_checkbox.get_attribute.assert_any_await("aria-checked", timeout=1000)

    asyncio.run(_run())


def test_download_audio_uses_timeout():
    """Verifies that download_audio initializes ClientSession with total=10.0 timeout."""

    async def _run():
        mock_page = MagicMock()
        solver = RecaptchaSolver(mock_page)

        mock_resp = AsyncMock()
        mock_resp.read = AsyncMock(return_value=b"fake audio bytes")

        mock_session = MagicMock()
        mock_session.get.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_session.get.return_value.__aexit__ = AsyncMock(return_value=None)

        with patch("aiohttp.ClientSession") as mock_session_cls:
            mock_session_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_session
            )
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=None)

            with patch("map_miner.recaptcha_solver._write_bytes_sync"):
                await solver.download_audio(
                    "http://example.com/audio.mp3", "/tmp/audio.mp3"
                )

            mock_session_cls.assert_called_once()
            timeout_arg = mock_session_cls.call_args.kwargs.get("timeout")
            assert timeout_arg is not None
            assert timeout_arg.total == 10.0

    asyncio.run(_run())


def test_multi_round_audio_captcha_does_not_reload():
    """Verifies that when 'Multiple correct solutions required' is present,
    the solver treats it as the next round and does NOT click reload button."""

    async def _run():
        mock_page = MagicMock()
        mock_challenge_frame = MagicMock()
        mock_page.frame_locator.return_value = mock_challenge_frame

        mock_resp_input = MagicMock()
        mock_resp_input.is_visible = AsyncMock(return_value=True)
        mock_resp_input.fill = AsyncMock()
        mock_resp_input.press = AsyncMock()

        mock_audio_src = MagicMock()
        mock_audio_src.count = AsyncMock(return_value=1)
        mock_audio_src.is_visible = AsyncMock(return_value=True)

        src_state = {"src": ""}

        async def mock_get_attribute(attr, *args, **kwargs):
            if attr == "src":
                return src_state["src"]
            return None

        mock_audio_src.get_attribute = AsyncMock(side_effect=mock_get_attribute)

        mock_body = MagicMock()
        mock_body.inner_text = AsyncMock(
            return_value="Multiple correct solutions required. Please solve more."
        )

        mock_reload_btn = MagicMock()
        mock_reload_btn.is_visible = AsyncMock(return_value=True)
        mock_reload_btn.click = AsyncMock()

        mock_verify_btn = MagicMock()
        mock_verify_btn.is_visible = AsyncMock(return_value=True)
        mock_verify_btn.click = AsyncMock()

        def challenge_locator(selector):
            if "#audio-response" in selector:
                return mock_resp_input
            if "#audio-source" in selector:
                return mock_audio_src
            if "body" in selector:
                return mock_body
            if "#recaptcha-reload-button" in selector:
                return mock_reload_btn
            if "#recaptcha-verify-button" in selector:
                return mock_verify_btn
            return MagicMock(
                count=AsyncMock(return_value=0),
                is_visible=AsyncMock(return_value=False),
            )

        mock_challenge_frame.locator.side_effect = challenge_locator
        solver = RecaptchaSolver(mock_page)
        solved_state = {"solved": False}

        async def mock_is_solved():
            return solved_state["solved"]

        def mock_transcribe(*args, **kwargs):
            solved_state["solved"] = True
            return "12345"

        fake_time = 0.0

        def fake_monotonic():
            nonlocal fake_time
            return fake_time

        async def fake_sleep(duration):
            nonlocal fake_time
            fake_time += duration
            if fake_time >= 4.0:
                src_state["src"] = (
                    "https://www.google.com/recaptcha/api2/payload?id=round2"
                )

        with (
            patch("time.monotonic", side_effect=fake_monotonic),
            patch("asyncio.sleep", side_effect=fake_sleep),
            patch.object(solver, "is_hard_blocked", AsyncMock(return_value=False)),
            patch.object(solver, "is_solved", side_effect=mock_is_solved),
            patch.object(solver, "download_audio", AsyncMock()) as mock_dl,
            patch("map_miner.recaptcha_solver.preprocess_audio"),
            patch(
                "map_miner.recaptcha_solver.transcribe_audio",
                side_effect=mock_transcribe,
            ),
        ):
            solved = await solver.solve_audio_captcha(max_audio_attempts=4)
            assert solved is True
            mock_reload_btn.click.assert_not_called()
            mock_dl.assert_called_with(
                "https://www.google.com/recaptcha/api2/payload?id=round2", ANY
            )

    asyncio.run(_run())


def test_normalize_audio_transcription():
    """Verifies that normalize_audio_transcription maps number words to digits and handles digit sequences."""
    assert normalize_audio_transcription("one four seven zero two") == "14702"
    assert normalize_audio_transcription("zero five eight nine") == "0589"
    assert normalize_audio_transcription("4 8 2 1") == "4821"
    assert normalize_audio_transcription("One, Two! Three.") == "123"
    assert normalize_audio_transcription("12345") == "12345"
    assert normalize_audio_transcription("one two three 123") == "123123"

    assert normalize_audio_transcription("hello world") == "hello world"
    assert (
        normalize_audio_transcription("press one to continue") == "press 1 to continue"
    )
    assert normalize_audio_transcription("oh one two three") == "0123"
    assert normalize_audio_transcription("zero oh seven") == "007"
    assert normalize_audio_transcription("eight oh eight") == "808"
    assert normalize_audio_transcription("") == ""
    assert normalize_audio_transcription("   ") == ""


def test_human_click_with_bounding_box():
    """Verifies that _human_click simulates natural mouse movement and clicks within the element's bounding box."""

    async def _run():
        mock_page = MagicMock()
        mock_page.mouse = MagicMock()
        mock_page.mouse.move = AsyncMock()
        mock_page.mouse.down = AsyncMock()
        mock_page.mouse.up = AsyncMock()

        mock_locator = MagicMock()
        mock_locator.bounding_box = AsyncMock(
            return_value={"x": 100, "y": 200, "width": 80, "height": 40}
        )

        solver = RecaptchaSolver(mock_page)
        clicked = await solver._human_click(mock_locator)
        assert clicked is True

        mock_page.mouse.move.assert_awaited_once()
        move_args = mock_page.mouse.move.call_args[0]
        x, y = move_args[0], move_args[1]
        assert 100 <= x <= 180
        assert 200 <= y <= 240
        assert "steps" in mock_page.mouse.move.call_args.kwargs
        mock_page.mouse.down.assert_awaited_once()
        mock_page.mouse.up.assert_awaited_once()

    asyncio.run(_run())


def test_human_click_fallback_without_bounding_box():
    """Verifies that _human_click falls back to standard click when bounding_box is None."""

    async def _run():
        mock_page = MagicMock()
        mock_locator = MagicMock()
        mock_locator.bounding_box = AsyncMock(return_value=None)
        mock_locator.click = AsyncMock()

        solver = RecaptchaSolver(mock_page)
        clicked = await solver._human_click(mock_locator)
        assert clicked is True
        mock_locator.click.assert_awaited_once()

    asyncio.run(_run())


def test_save_captcha_diagnostics(tmp_path):
    """Verifies that save_captcha_diagnostics creates diagnostic folder,
    saves screenshot, page.html, and meta.json with sitekey, data-s, user agent, cookies."""
    html_sample = (
        "<html><head></head><body>"
        '<iframe src="https://www.google.com/recaptcha/api2/bframe?k=6LfwuyUTAAAAAOAmoS9nnsqAlwPh&s=ABCDEF12345"></iframe>'
        "</body></html>"
    )
    diag_paths: list[str] = []

    async def _run():
        mock_page = MagicMock()
        mock_page.url = (
            "https://www.google.com/sorry/index?k=6LfwuyUTAAAAAOAmoS9nnsqAlwPh"
        )
        mock_page.screenshot = AsyncMock()
        mock_page.content = AsyncMock(return_value=html_sample)
        mock_page.evaluate = AsyncMock(return_value="Mozilla/5.0 Custom UA")

        mock_context = MagicMock()
        mock_context.cookies = AsyncMock(
            return_value=[{"name": "NID", "value": "12345"}]
        )
        mock_page.context = mock_context

        debug_dir = str(tmp_path / "debug_captchas")
        solver = RecaptchaSolver(mock_page, debug_dir=debug_dir)

        diag_path = await solver.save_captcha_diagnostics(context_label="test_diag")
        diag_paths.append(diag_path)
        mock_page.screenshot.assert_awaited_once()

        # Test standalone function
        diag_path_standalone = await save_captcha_diagnostics(
            mock_page, context_label="standalone", debug_dir=debug_dir
        )
        diag_paths.append(diag_path_standalone)

    asyncio.run(_run())

    diag_path = diag_paths[0]
    assert os.path.exists(diag_path)
    assert os.path.exists(os.path.join(diag_path, "page.html"))
    assert os.path.exists(os.path.join(diag_path, "meta.json"))

    with open(os.path.join(diag_path, "page.html"), encoding="utf-8") as f:
        saved_html = f.read()
        assert saved_html == html_sample

    with open(os.path.join(diag_path, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
        assert meta["context_label"] == "test_diag"
        assert meta["sitekey"] == "6LfwuyUTAAAAAOAmoS9nnsqAlwPh"
        assert meta["data-s"] == "ABCDEF12345"
        assert meta["user_agent"] == "Mozilla/5.0 Custom UA"
        assert len(meta["cookies"]) == 1
        assert meta["cookies"][0]["name"] == "NID"

    assert os.path.exists(diag_paths[1])
    assert os.path.exists(os.path.join(diag_paths[1], "meta.json"))


def test_transcribe_audio_google_en_us():
    """Verifies that transcribe_audio passes language='en-US' explicitly to recognize_google."""
    with (
        patch("speech_recognition.AudioFile"),
        patch("speech_recognition.Recognizer") as mock_recognizer_cls,
    ):
        mock_recognizer = MagicMock()
        mock_recognizer_cls.return_value = mock_recognizer
        mock_recognizer.recognize_google.return_value = "seven three four"

        result = transcribe_audio("test.wav", engine="google")
        assert result == "seven three four"
        mock_recognizer.recognize_google.assert_called_once_with(ANY, language="en-US")
