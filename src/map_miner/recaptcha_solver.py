import asyncio
import json
import logging
import os
import random
import re
import tempfile
import time
from typing import Any

import aiohttp
import speech_recognition as sr
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)
from pydub import AudioSegment

logger = logging.getLogger(__name__)

__all__ = [
    "CHALLENGE_FRAME_SELECTOR",
    "RecaptchaBlockedError",
    "RecaptchaError",
    "RecaptchaSolveError",
    "RecaptchaSolver",
    "normalize_audio_transcription",
    "preprocess_audio",
    "save_captcha_diagnostics",
    "transcribe_audio",
]

CHALLENGE_FRAME_SELECTOR = (
    'iframe[title*="challenge"], '
    'iframe[src*="google.com/recaptcha/api2/bframe"], '
    'iframe[src*="/bframe"]'
)


class RecaptchaError(Exception):
    """Base exception for reCAPTCHA errors."""


class RecaptchaBlockedError(RecaptchaError):
    """Raised when Google blocks the reCAPTCHA challenge (e.g. automated queries detected)."""


class RecaptchaSolveError(RecaptchaError):
    """Raised when reCAPTCHA cannot be solved after maximum attempts."""


WORD_TO_DIGIT: dict[str, str] = {
    "zero": "0",
    "oh": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
}


def normalize_audio_transcription(text: str) -> str:
    """Normalizes transcribed audio text:
    - Converts to lowercase and removes punctuation
    - Converts number words ('zero'..'nine') to numeric digits ('0'..'9')
    - If all tokens are digits, concatenates them into a continuous digit sequence

    Args:
        text (str): Raw transcription text.

    Returns:
        str: Normalized text or digit sequence.
    """
    if not text:
        return ""

    cleaned = text.lower().strip()
    cleaned = re.sub(r"[^\w\s]", " ", cleaned)
    tokens = cleaned.split()
    if not tokens:
        return ""

    normalized_tokens = [WORD_TO_DIGIT.get(tok, tok) for tok in tokens]

    if all(tok.isdigit() for tok in normalized_tokens):
        return "".join(normalized_tokens)

    return " ".join(normalized_tokens)


def _write_bytes_sync(path: str, data: bytes) -> None:
    """Helper to write binary data to disk in a separate thread."""
    with open(path, "wb") as f:
        f.write(data)


def _write_text_sync(path: str, text: str) -> None:
    """Helper to write text data to disk in a separate thread."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def preprocess_audio(mp3_path: str, wav_path: str) -> None:
    """Preprocesses MP3 audio for speech recognition:

    Converts to 16kHz mono, normalizes volume, and applies 300Hz-3400Hz band-pass filter.
    Outputs standard 16-bit PCM WAV.
    """
    sound = AudioSegment.from_mp3(mp3_path)
    processed = (
        sound.set_frame_rate(16000)
        .set_channels(1)
        .normalize()
        .high_pass_filter(300)
        .low_pass_filter(3400)
    )
    processed.export(wav_path, format="wav")


def transcribe_audio(wav_path: str, engine: str = "auto") -> str:
    """Transcribes WAV audio using Whisper, Google, or Vosk STT engines with automatic fallback.

    Args:
        wav_path (str): Path to preprocessed WAV audio file.
        engine (str): Engine to use ('auto', 'whisper', 'google', 'vosk'). Defaults to 'auto'.

    Returns:
        str: Normalized lowercase text with weird punctuation removed.

    Raises:
        RecaptchaSolveError: If all speech recognition attempts fail or transcription is empty.
    """
    recognizer = sr.Recognizer()
    with sr.AudioFile(wav_path) as source:
        audio = recognizer.record(source)

    text: str | None = None
    errors: list[str] = []
    engine_used: str | None = None

    def _try_whisper() -> str | None:
        try:
            whisper_fn = getattr(recognizer, "recognize_whisper", None)
            if whisper_fn is None:
                errors.append("whisper: recognize_whisper not available")
                return None
            res = str(whisper_fn(audio, model="tiny"))
            if res and res.strip():
                return res.strip()
            return None
        except (ImportError, Exception) as e:  # noqa: BLE001
            errors.append(f"whisper: {e}")
            return None

    def _try_google() -> str | None:
        try:
            google_fn = getattr(recognizer, "recognize_google", None)
            if google_fn is None:
                errors.append("google: recognize_google not available")
                return None
            res = str(google_fn(audio, language="en-US"))
            if res and res.strip():
                return res.strip()
            return None
        except (sr.UnknownValueError, sr.RequestError, Exception) as e:  # noqa: BLE001
            errors.append(f"google: {e}")
            return None

    def _try_vosk() -> str | None:
        try:
            res = recognizer.recognize_vosk(audio)
            if isinstance(res, str) and "text" in res:
                try:
                    data = json.loads(res)
                    v_text = str(data.get("text", res)).strip()
                    return v_text if v_text else None
                except json.JSONDecodeError:
                    return res.strip() if res.strip() else None
            return str(res).strip() if res and str(res).strip() else None
        except (ImportError, Exception) as e:  # noqa: BLE001
            errors.append(f"vosk: {e}")
            return None

    if engine == "whisper":
        text = _try_whisper()
        if text:
            engine_used = "whisper"
    elif engine == "google":
        text = _try_google()
        if text:
            engine_used = "google"
    elif engine == "vosk":
        text = _try_vosk()
        if text:
            engine_used = "vosk"
    elif engine == "auto":
        text = _try_whisper()
        if text:
            engine_used = "whisper"
        if not text:
            text = _try_google()
            if text:
                engine_used = "google"
        if not text:
            text = _try_vosk()
            if text:
                engine_used = "vosk"
    else:
        raise ValueError(f"Unsupported transcription engine: {engine}")

    if not text:
        msg = f"Speech recognition failed: {'; '.join(errors)}"
        logger.warning("%s", msg)
        raise RecaptchaSolveError(msg)

    logger.debug(
        "Speech recognition succeeded using engine '%s': %s",
        engine_used,
        text,
    )

    # Post-processing: lowercase, remove non-alphanumeric punctuation, strip excess whitespace
    cleaned = text.lower()
    cleaned = re.sub(r"[^\w\s]", "", cleaned)
    cleaned = " ".join(cleaned.split()).strip()

    if not cleaned:
        raise RecaptchaSolveError("Cleaned transcription is empty")

    return cleaned


class RecaptchaSolver:
    """Automated solver for Google reCAPTCHA v2 (checkbox and audio challenge)

    using Playwright, pydub, and multi-engine Speech Recognition.
    """

    def __init__(
        self,
        page: Any,
        debug_dir: str = "debug/captchas",
        debug: bool = False,
    ) -> None:
        self.page = page
        self.debug_dir = debug_dir
        self.debug = debug

    async def is_hard_blocked(self, challenge_frame: Any = None) -> bool:
        """Checks if Google has hard-blocked the audio challenge with automated query detection."""
        try:
            if challenge_frame is not None:
                dos_loc = challenge_frame.locator(
                    ".rc-doscaptcha-body, .rc-doscaptcha-header, .rc-doscaptcha-header-text, #recaptcha-audio-button[disabled]"
                )
                if asyncio.iscoroutine(dos_loc):
                    dos_loc = await dos_loc
                if await dos_loc.count() > 0:
                    return True

                body_loc = challenge_frame.locator("body")
                if asyncio.iscoroutine(body_loc):
                    body_loc = await body_loc
                if await body_loc.count() > 0:
                    try:
                        inner_text_res = body_loc.inner_text(timeout=1000)
                    except TypeError:
                        inner_text_res = body_loc.inner_text()
                    body_text = (
                        await inner_text_res
                        if asyncio.iscoroutine(inner_text_res)
                        else str(inner_text_res)
                    ).lower()
                    blocked_phrases = [
                        "automated queries",
                        "can't process your request right now",
                        "cannot process your request right now",
                        "try again later",
                    ]
                    if any(phrase in body_text for phrase in blocked_phrases):
                        return True

            page_dos = self.page.locator(
                ".rc-doscaptcha-body, .rc-doscaptcha-header, .rc-doscaptcha-header-text"
            )
            if asyncio.iscoroutine(page_dos):
                page_dos = await page_dos
            if await page_dos.count() > 0:
                return True
        except (PlaywrightError, OSError) as e:
            logger.debug("Error checking hard block status: %s", e)
        return False

    async def _handle_sorry_page_redirect(self) -> None:
        """If on Google Sorry page ('sorry/index'), submits the form and waits for redirection."""
        if "sorry/index" in self.page.url:
            logger.info("Detected Google Sorry page. Submitting form to proceed...")
            try:
                submit_btn = self.page.locator(
                    'form input[type="submit"], form button[type="submit"], #captcha-form input[type="submit"]'
                )
                if asyncio.iscoroutine(submit_btn):
                    submit_btn = await submit_btn
                if await submit_btn.count() > 0 and await submit_btn.first.is_visible():
                    await submit_btn.first.click()
                else:
                    await self.page.evaluate(
                        "() => { const f = document.querySelector('form'); if (f) f.submit(); }"
                    )
                await self.page.wait_for_url(
                    lambda u: "sorry/index" not in u, timeout=10000
                )
                logger.info("🎉 Google Sorry page form submitted and redirected back.")
            except (PlaywrightError, OSError) as e:
                logger.warning("Issue during sorry page redirect: %s", e)

    async def save_captcha_diagnostics(self, context_label: str = "") -> str:
        """Saves debugging diagnostics (screenshot, page HTML, metadata) when CAPTCHA is encountered.

        Creates directory: {debug_dir}/captcha_{timestamp}_{rand}/
        Saves:
        - screenshot.png
        - page.html
        - meta.json (sitekey, data-s, timestamp, context_label, user_agent, cookies, url)

        Returns:
            str: Path to the diagnostics directory.
        """
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        rand_suffix = f"{random.randint(1000, 9999)}"
        diag_dir = os.path.join(self.debug_dir, f"captcha_{timestamp}_{rand_suffix}")
        os.makedirs(diag_dir, exist_ok=True)

        # 1. Screenshot
        screenshot_path = os.path.join(diag_dir, "screenshot.png")
        try:
            if hasattr(self.page, "screenshot"):
                res = self.page.screenshot(path=screenshot_path)
                if asyncio.iscoroutine(res):
                    await res
        except (PlaywrightError, OSError) as e:
            logger.debug("Failed to capture diagnostics screenshot: %s", e)

        # 2. HTML content
        html_path = os.path.join(diag_dir, "page.html")
        html_content = ""
        try:
            if hasattr(self.page, "content"):
                res = self.page.content()
                html_content = await res if asyncio.iscoroutine(res) else str(res)
            await asyncio.to_thread(_write_text_sync, html_path, html_content)
        except (PlaywrightError, OSError) as e:
            logger.debug("Failed to capture diagnostics HTML: %s", e)

        # 3. Metadata
        meta_path = os.path.join(diag_dir, "meta.json")
        sitekey = None
        data_s = None
        user_agent = None
        cookies = []

        try:
            if hasattr(self.page, "evaluate"):
                ua_res = self.page.evaluate("navigator.userAgent")
                user_agent = (
                    await ua_res if asyncio.iscoroutine(ua_res) else str(ua_res)
                )
        except Exception as e:  # noqa: BLE001
            logger.debug("Failed to evaluate userAgent for diagnostics: %s", e)

        try:
            ctx = getattr(self.page, "context", None)
            if ctx and hasattr(ctx, "cookies"):
                c_res = ctx.cookies()
                cookies = await c_res if asyncio.iscoroutine(c_res) else c_res
        except Exception as e:  # noqa: BLE001
            logger.debug("Failed to retrieve cookies for diagnostics: %s", e)

        # Extract sitekey from iframe src or page content
        sitekey_match = re.search(r"[?&]k=([a-zA-Z0-9_-]+)", html_content)
        if sitekey_match:
            sitekey = sitekey_match.group(1)
        else:
            sitekey_match2 = re.search(
                r'data-sitekey=["\']([a-zA-Z0-9_-]+)["\']', html_content
            )
            if sitekey_match2:
                sitekey = sitekey_match2.group(1)

        # Extract data-s from iframe src or page content
        data_s_match = re.search(r'data-s=["\']([^"\']+)["\']', html_content)
        if data_s_match:
            data_s = data_s_match.group(1)
        else:
            data_s_match2 = re.search(r"[?&]s=([a-zA-Z0-9_-]+)", html_content)
            if data_s_match2:
                data_s = data_s_match2.group(1)

        meta = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "context_label": context_label,
            "url": getattr(self.page, "url", ""),
            "user_agent": user_agent,
            "sitekey": sitekey,
            "data-s": data_s,
            "cookies": cookies,
        }

        try:
            meta_json = json.dumps(meta, indent=2)
            await asyncio.to_thread(_write_text_sync, meta_path, meta_json)
        except OSError as e:
            logger.debug("Failed to write meta.json: %s", e)

        logger.info("Saved CAPTCHA diagnostics to: %s", diag_dir)
        return diag_dir

    async def download_audio(self, url: str, path: str) -> None:
        """Downloads audio file asynchronously from URL to local file path."""
        timeout = aiohttp.ClientTimeout(total=10.0)
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(url) as response,
        ):
            content = await response.read()
            await asyncio.to_thread(_write_bytes_sync, path, content)
        logger.debug("Downloaded audio file to: %s", path)

    async def solve_captcha(self) -> bool:
        """Attempts to solve Google reCAPTCHA v2 (checkbox or audio challenge)."""
        try:
            if await self.is_solved():
                logger.info("🎉 CAPTCHA already solved.")
                await self._handle_sorry_page_redirect()
                return True

            recaptcha_frame = self.page.frame_locator('iframe[title*="reCAPTCHA"]')
            checkbox = recaptcha_frame.locator("#recaptcha-anchor")
            if await checkbox.count() > 0:
                await self._human_click(checkbox)
                await asyncio.sleep(1.0)

            if await self.is_solved():
                logger.info("🎉 CAPTCHA solved by checkbox click.")
                await self._handle_sorry_page_redirect()
                return True

            solved = await self.solve_audio_captcha()
            if solved:
                await self._handle_sorry_page_redirect()
                return True
            return False

        except RecaptchaBlockedError:
            raise
        except RecaptchaError:
            raise
        except Exception as e:
            logger.error("An error occurred while solving CAPTCHA: %s", e)
            raise RecaptchaSolveError(
                f"Unexpected error while solving CAPTCHA: {e}"
            ) from e

    async def _safe_is_visible(self, locator: Any) -> bool:
        """Safely checks if a locator is visible, handling mock and coroutine differences."""
        try:
            if asyncio.iscoroutine(locator):
                locator = await locator
            if hasattr(locator, "is_visible"):
                res = locator.is_visible()
                if asyncio.iscoroutine(res):
                    return bool(await res)
                return bool(res)
        except (PlaywrightError, OSError, TypeError, AttributeError) as e:
            logger.debug("Safe visibility check encountered error: %s", e)
        return False

    async def _safe_click(self, locator: Any) -> bool:
        """Safely clicks a locator, handling mock and coroutine differences."""
        try:
            if asyncio.iscoroutine(locator):
                locator = await locator
            if hasattr(locator, "click"):
                res = locator.click()
                if asyncio.iscoroutine(res):
                    await res
                return True
        except (PlaywrightError, OSError, TypeError, AttributeError) as e:
            logger.debug("Safe click encountered error: %s", e)
        return False

    async def _human_click(self, locator: Any) -> bool:
        """Simulates human mouse movement and click with natural trajectory and randomized delays."""
        try:
            if asyncio.iscoroutine(locator):
                locator = await locator

            box = None
            if hasattr(locator, "bounding_box"):
                res = locator.bounding_box()
                box = await res if asyncio.iscoroutine(res) else res

            if box and isinstance(box, dict) and "x" in box and "width" in box:
                x = box["x"] + box["width"] * random.uniform(0.25, 0.75)
                y = box["y"] + box["height"] * random.uniform(0.25, 0.75)
                steps = random.randint(5, 12)
                if hasattr(self.page, "mouse"):
                    move_res = self.page.mouse.move(x, y, steps=steps)
                    if asyncio.iscoroutine(move_res):
                        await move_res
                    await asyncio.sleep(random.uniform(0.05, 0.15))
                    down_res = self.page.mouse.down()
                    if asyncio.iscoroutine(down_res):
                        await down_res
                    await asyncio.sleep(random.uniform(0.05, 0.12))
                    up_res = self.page.mouse.up()
                    if asyncio.iscoroutine(up_res):
                        await up_res
                    return True

            return await self._safe_click(locator)
        except (PlaywrightError, OSError, TypeError, AttributeError) as e:
            logger.debug("Human click error: %s, falling back to safe click", e)
            return await self._safe_click(locator)

    async def _safe_wait_for(
        self, locator: Any, state: str = "visible", timeout: float = 5000
    ) -> None:
        """Safely waits for a locator state, handling mock and coroutine differences."""
        try:
            if asyncio.iscoroutine(locator):
                locator = await locator
            if hasattr(locator, "wait_for"):
                res = locator.wait_for(state=state, timeout=timeout)
                if asyncio.iscoroutine(res):
                    await res
        except (PlaywrightTimeoutError, PlaywrightError):
            raise
        except (OSError, TypeError, AttributeError) as e:
            logger.debug("Safe wait_for encountered error: %s", e)

    async def solve_audio_captcha(self, max_audio_attempts: int = 5) -> bool:
        """Solves reCAPTCHA v2 audio challenge using speech recognition."""
        with tempfile.TemporaryDirectory(prefix="captcha_audio_") as tmp_dir:
            return await self._process_audio_challenge(
                tmp_dir, max_audio_attempts=max_audio_attempts
            )

    async def _process_audio_challenge(
        self, target_dir: str, max_audio_attempts: int = 5
    ) -> bool:
        """Internal worker to process audio challenges, supporting multi-round solving."""
        try:
            challenge_frame = self.page.frame_locator(CHALLENGE_FRAME_SELECTOR)
            if asyncio.iscoroutine(challenge_frame):
                challenge_frame = await challenge_frame

            last_audio_source: str | None = None

            for attempt in range(1, max_audio_attempts + 1):
                logger.info(
                    "Processing audio CAPTCHA round %d/%d...",
                    attempt,
                    max_audio_attempts,
                )

                # 1. Switch to audio challenge if not already active
                in_audio_challenge = False
                try:
                    resp_input = challenge_frame.locator("#audio-response")
                    src_elem = challenge_frame.locator("#audio-source")
                    if await self._safe_is_visible(
                        resp_input
                    ) or await self._safe_is_visible(src_elem):
                        in_audio_challenge = True
                except (PlaywrightError, OSError, TypeError, AttributeError) as e:
                    logger.debug("Error checking initial audio challenge state: %s", e)
                    in_audio_challenge = False

                if not in_audio_challenge:
                    audio_btn = challenge_frame.locator("#recaptcha-audio-button")
                    if asyncio.iscoroutine(audio_btn):
                        audio_btn = await audio_btn
                    try:
                        await self._safe_wait_for(
                            audio_btn, state="visible", timeout=5000
                        )
                        await self._human_click(audio_btn)
                        await asyncio.sleep(1.0)
                    except (PlaywrightTimeoutError, PlaywrightError) as e:
                        logger.debug("reCAPTCHA audio button wait/click error: %s", e)

                if await self.is_hard_blocked(challenge_frame):
                    logger.warning(
                        "🚨 Google hard-blocked audio challenge: automated queries detected."
                    )
                    raise RecaptchaBlockedError(
                        "Your computer or network may be sending automated queries"
                    )

                # 2. Polling for audio source URL and multi-round handling
                audio_source_loc = challenge_frame.locator("#audio-source")
                if asyncio.iscoroutine(audio_source_loc):
                    audio_source_loc = await audio_source_loc

                audio_source: str | None = None
                poll_timeout = 8.0
                poll_interval = 0.5
                start_time = time.monotonic()
                reloaded = False

                while (time.monotonic() - start_time) < poll_timeout:
                    if await self.is_hard_blocked(challenge_frame):
                        raise RecaptchaBlockedError(
                            "Your computer or network may be sending automated queries"
                        )
                    if await self.is_solved():
                        logger.info("🎉 CAPTCHA verified as solved during polling.")
                        return True

                    try:
                        has_source = False
                        if hasattr(audio_source_loc, "count"):
                            cnt_res = audio_source_loc.count()
                            cnt = (
                                await cnt_res
                                if asyncio.iscoroutine(cnt_res)
                                else cnt_res
                            )
                            has_source = cnt > 0

                        if has_source and hasattr(audio_source_loc, "get_attribute"):
                            try:
                                attr_res = audio_source_loc.get_attribute(
                                    "src", timeout=1000
                                )
                            except TypeError:
                                attr_res = audio_source_loc.get_attribute("src")
                            src = (
                                await attr_res
                                if asyncio.iscoroutine(attr_res)
                                else attr_res
                            )
                            if src and (
                                last_audio_source is None or src != last_audio_source
                            ):
                                audio_source = str(src).strip()
                                break
                    except (
                        PlaywrightError,
                        OSError,
                        TypeError,
                        AttributeError,
                    ) as e:
                        logger.debug("Error retrieving #audio-source src: %s", e)

                    elapsed = time.monotonic() - start_time
                    # Check for Google retry messages and trigger reload if new audio not loaded
                    if elapsed >= 3.0 and not reloaded:
                        is_multi_round = False
                        is_retry_needed = False
                        try:
                            body_loc = challenge_frame.locator("body")
                            if asyncio.iscoroutine(body_loc):
                                body_loc = await body_loc
                            if hasattr(body_loc, "inner_text"):
                                try:
                                    it = body_loc.inner_text(timeout=1000)
                                except TypeError:
                                    it = body_loc.inner_text()
                                txt = (
                                    await it if asyncio.iscoroutine(it) else str(it)
                                ).lower()
                                if "multiple correct solutions" in txt:
                                    is_multi_round = True
                                elif "please try again" in txt:
                                    is_retry_needed = True
                        except (
                            PlaywrightError,
                            OSError,
                            TypeError,
                            AttributeError,
                        ) as e:
                            logger.debug("Error reading challenge body text: %s", e)

                        if is_multi_round:
                            logger.info(
                                "Multi-round challenge in progress ('multiple correct solutions required'). Waiting for new audio..."
                            )
                        elif is_retry_needed:
                            reload_btn = challenge_frame.locator(
                                "#recaptcha-reload-button"
                            )
                            if await self._safe_is_visible(reload_btn):
                                logger.info(
                                    "Detected reCAPTCHA retry message ('please try again'). Clicking reload button..."
                                )
                                await self._human_click(reload_btn)
                                reloaded = True
                                await asyncio.sleep(1.0)
                                continue

                    await asyncio.sleep(poll_interval)

                if not audio_source:
                    if await self.is_hard_blocked(challenge_frame):
                        raise RecaptchaBlockedError(
                            "Your computer or network may be sending automated queries"
                        )
                    if await self.is_solved():
                        return True
                    if attempt == max_audio_attempts:
                        raise RecaptchaSolveError(
                            "Audio challenge source URL is missing or not updated"
                        )
                    continue

                logger.info("Audio challenge source URL: %s", audio_source)

                path_to_mp3 = os.path.join(target_dir, f"audio_{attempt}.mp3")
                path_to_wav = os.path.join(target_dir, f"audio_{attempt}.wav")

                await self.download_audio(audio_source, path_to_mp3)
                await asyncio.sleep(random.uniform(1.5, 2.5))
                await asyncio.to_thread(preprocess_audio, path_to_mp3, path_to_wav)
                logger.debug("Preprocessed MP3 to 16kHz mono WAV.")

                try:
                    captcha_text = await asyncio.to_thread(
                        transcribe_audio, path_to_wav, "auto"
                    )
                except RecaptchaSolveError:
                    # Resilient fallback: try raw 16kHz mono WAV without aggressive bandpass filter
                    path_to_raw_wav = os.path.join(
                        target_dir, f"audio_{attempt}_raw.wav"
                    )

                    def _export_raw_wav(
                        src: str = path_to_mp3, dst: str = path_to_raw_wav
                    ) -> None:
                        snd = AudioSegment.from_mp3(src)
                        snd.set_frame_rate(16000).set_channels(1).export(
                            dst, format="wav"
                        )

                    await asyncio.to_thread(_export_raw_wav)
                    captcha_text = await asyncio.to_thread(
                        transcribe_audio, path_to_raw_wav, "auto"
                    )
                captcha_text = normalize_audio_transcription(captcha_text)
                logger.info("Recognized CAPTCHA text: %s", captcha_text)

                # 3. Submit response
                resp_loc = challenge_frame.locator("#audio-response")
                if asyncio.iscoroutine(resp_loc):
                    resp_loc = await resp_loc

                await self._human_click(resp_loc)
                await asyncio.sleep(random.uniform(0.2, 0.4))

                typed = False
                if hasattr(resp_loc, "press_sequentially"):
                    try:
                        res = resp_loc.press_sequentially(
                            captcha_text, delay=random.randint(70, 130)
                        )
                        if asyncio.iscoroutine(res):
                            await res
                        typed = True
                    except (PlaywrightError, OSError, TypeError, AttributeError) as e:
                        logger.debug("press_sequentially error: %s", e)

                if not typed and hasattr(resp_loc, "type"):
                    try:
                        res = resp_loc.type(captcha_text, delay=random.randint(70, 130))
                        if asyncio.iscoroutine(res):
                            await res
                        typed = True
                    except (PlaywrightError, OSError, TypeError, AttributeError):
                        pass

                if not typed and hasattr(resp_loc, "fill"):
                    fill_res = resp_loc.fill(captcha_text)
                    if asyncio.iscoroutine(fill_res):
                        await fill_res

                await asyncio.sleep(random.uniform(0.5, 1.0))

                verify_btn = challenge_frame.locator("#recaptcha-verify-button")
                if asyncio.iscoroutine(verify_btn):
                    verify_btn = await verify_btn

                verify_clicked = False
                if await self._safe_is_visible(verify_btn):
                    verify_clicked = await self._human_click(verify_btn)

                if not verify_clicked and hasattr(resp_loc, "press"):
                    press_res = resp_loc.press("Enter")
                    if asyncio.iscoroutine(press_res):
                        await press_res

                last_audio_source = audio_source
                logger.debug(
                    "Entered and submitted CAPTCHA text, waiting 1.5s for validation."
                )
                await asyncio.sleep(1.5)

                if await self.is_solved():
                    logger.info(
                        "🎉 Audio CAPTCHA solved successfully on round %d.", attempt
                    )
                    return True

                if await self.is_hard_blocked(challenge_frame):
                    raise RecaptchaBlockedError(
                        "Your computer or network may be sending automated queries"
                    )

                logger.info(
                    "reCAPTCHA requires further verification (completed round %d/%d).",
                    attempt,
                    max_audio_attempts,
                )

            logger.warning(
                "❌ Failed to solve audio CAPTCHA after %d attempts.",
                max_audio_attempts,
            )
            raise RecaptchaSolveError(
                f"Failed to solve audio CAPTCHA after {max_audio_attempts} attempts"
            )

        except (RecaptchaBlockedError, RecaptchaSolveError):
            raise
        except Exception as e:
            logger.error("An error occurred while solving audio CAPTCHA: %s", e)
            raise

    async def is_solved(self) -> bool:
        """Checks if the reCAPTCHA checkbox is marked as checked."""
        try:
            recaptcha_frame = self.page.frame_locator('iframe[title*="reCAPTCHA"]')
            if asyncio.iscoroutine(recaptcha_frame):
                recaptcha_frame = await recaptcha_frame
            checkbox = recaptcha_frame.locator("#recaptcha-anchor")
            if asyncio.iscoroutine(checkbox):
                checkbox = await checkbox
            if await checkbox.count() == 0:
                return False

            try:
                aria_res = checkbox.get_attribute("aria-checked", timeout=1000)
            except TypeError:
                aria_res = checkbox.get_attribute("aria-checked")
            aria_checked = await aria_res if asyncio.iscoroutine(aria_res) else aria_res

            try:
                class_res = checkbox.get_attribute("class", timeout=1000)
            except TypeError:
                class_res = checkbox.get_attribute("class")
            checkbox_class = (
                await class_res if asyncio.iscoroutine(class_res) else class_res
            )

            return aria_checked == "true" or "recaptcha-checkbox-checked" in (
                checkbox_class or ""
            )
        except PlaywrightError as e:
            logger.debug("Error checking if CAPTCHA is solved: %s", e)
            return False

    # Backward compatibility aliases
    solveCaptcha = solve_captcha
    solveAudioCaptcha = solve_audio_captcha
    isSolved = is_solved


async def save_captcha_diagnostics(
    page: Any, context_label: str = "", debug_dir: str = "debug/captchas"
) -> str:
    """Standalone helper to capture and save CAPTCHA diagnostics."""
    solver = RecaptchaSolver(page, debug_dir=debug_dir)
    return await solver.save_captcha_diagnostics(context_label=context_label)
