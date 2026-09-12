import asyncio
import json
import logging
import os
import random
import re
import tempfile
import time
from datetime import UTC, datetime
from typing import Any

import aiohttp
import speech_recognition as sr
from playwright.async_api import Error as PlaywrightError
from pydub import AudioSegment

logger = logging.getLogger(__name__)


def _write_bytes_sync(path: str, data: bytes) -> None:
    """Helper to write binary data to disk in a separate thread."""
    with open(path, "wb") as f:
        f.write(data)


def _write_text_sync(path: str, text: str) -> None:
    """Helper to write text data to disk in a separate thread."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class RecaptchaSolver:
    """
    Automated solver for Google reCAPTCHA v2 (checkbox and audio challenge)
    using Playwright, pydub, and Google Speech Recognition.
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
        self.current_diag_dir: str | None = None
        self.metadata: dict[str, Any] = {}

    def _get_diag_dir(self) -> str:
        if not self.current_diag_dir:
            now_str = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
            rand_suffix = random.randint(1000, 9999)
            self.current_diag_dir = os.path.join(
                self.debug_dir, f"captcha_{now_str}_{rand_suffix}"
            )
            os.makedirs(self.current_diag_dir, exist_ok=True)
        return self.current_diag_dir

    async def _save_metadata_file(self) -> None:
        if not self.current_diag_dir:
            return
        meta_path = os.path.join(self.current_diag_dir, "meta.json")
        try:
            content = json.dumps(self.metadata, indent=2, ensure_ascii=False)
            await asyncio.to_thread(_write_text_sync, meta_path, content)
        except OSError as e:
            logger.debug("Error writing meta.json: %s", e)

    async def save_captcha_diagnostics(self, reason: str = "detected") -> str:
        """
        Saves diagnostic information when a CAPTCHA is encountered:
        - Full page screenshot (screenshot.png)
        - Raw HTML source (page.html)
        - Metadata JSON (meta.json) containing sitekey, data-s, cookies, user-agent, form fields, IP, timestamp.
        """
        diag_dir = self._get_diag_dir()
        logger.info("💾 Saving CAPTCHA diagnostic data into: %s", diag_dir)

        # 1. Capture full-page screenshot
        screenshot_path = os.path.join(diag_dir, "screenshot.png")
        try:
            await self.page.screenshot(path=screenshot_path, full_page=True)
        except (PlaywrightError, OSError) as e:
            logger.debug("Failed to capture screenshot: %s", e)

        # 2. Capture complete HTML source
        html_content = ""
        html_path = os.path.join(diag_dir, "page.html")
        try:
            html_content = await self.page.content()
            await asyncio.to_thread(_write_text_sync, html_path, html_content)
        except (PlaywrightError, OSError) as e:
            logger.debug("Failed to capture HTML content: %s", e)

        # 3. Extract sitekey, data-s, and other security parameters
        sitekey = None
        data_s = None
        try:
            for frame in self.page.frames:
                m_k = re.search(r"[?&]k=([a-zA-Z0-9_-]+)", frame.url)
                if m_k:
                    sitekey = m_k.group(1)
                m_s = re.search(r"[?&]s=([a-zA-Z0-9_-]+)", frame.url)
                if m_s:
                    data_s = m_s.group(1)

            if not sitekey:
                m_k = re.search(
                    r'data-sitekey=["\']([a-zA-Z0-9_-]+)["\']', html_content
                )
                if m_k:
                    sitekey = m_k.group(1)

            if not data_s:
                m_s = re.search(r'data-s=["\']([a-zA-Z0-9_-]+)["\']', html_content)
                if m_s:
                    data_s = m_s.group(1)
        except (PlaywrightError, re.error) as e:
            logger.debug("Error extracting sitekey/data-s: %s", e)

        # 4. Extract IP address & block time if shown on sorry page
        ip_match = re.search(r"IP address:\s*([a-fA-F0-9:.]+)", html_content)
        ip_address = ip_match.group(1) if ip_match else None

        time_match = re.search(r"Time:\s*([^\s<]+)", html_content)
        block_time = time_match.group(1) if time_match else None

        # 5. Extract form inputs (e.g. continue, q)
        form_data = None
        try:
            form_data = await self.page.evaluate("""() => {
                const form = document.querySelector('form');
                if (!form) return null;
                const inputs = {};
                form.querySelectorAll('input').forEach(inp => {
                    if (inp.name) inputs[inp.name] = inp.value;
                });
                return {
                    action: form.getAttribute('action') || '',
                    method: form.getAttribute('method') || 'GET',
                    inputs: inputs
                };
            }""")
        except PlaywrightError as e:
            logger.debug("Error extracting form inputs: %s", e)

        # 6. Extract cookies, User-Agent & viewport
        cookies = []
        try:
            cookies = await self.page.context.cookies()
        except PlaywrightError as e:
            logger.debug("Error retrieving cookies: %s", e)

        user_agent = ""
        try:
            user_agent = await self.page.evaluate("() => navigator.userAgent")
        except PlaywrightError as e:
            logger.debug("Error retrieving userAgent: %s", e)

        # 7. Build metadata dictionary
        self.metadata = {
            "timestamp": datetime.now(UTC).isoformat(),
            "epoch": int(time.time()),
            "url": self.page.url,
            "title": await self.page.title() if not self.page.is_closed() else "",
            "detection_reason": reason,
            "ip_address": ip_address,
            "block_time": block_time,
            "sitekey": sitekey,
            "data_s": data_s,
            "form": form_data,
            "user_agent": user_agent,
            "viewport": self.page.viewport_size,
            "cookies": cookies,
            "audio": {},
            "solution": {
                "attempted": False,
                "solved": False,
                "method": None,
                "error": None,
            },
            "files": {
                "screenshot": "screenshot.png",
                "html": "page.html",
            },
        }

        await self._save_metadata_file()
        logger.info(
            "✅ Saved CAPTCHA diagnostic info: sitekey=%s, data_s=%s, ip=%s",
            sitekey,
            "yes" if data_s else "no",
            ip_address,
        )
        return diag_dir

    async def download_audio(self, url: str, path: str) -> None:
        """Downloads audio file asynchronously from URL to local file path."""
        async with aiohttp.ClientSession() as session, session.get(url) as response:
            content = await response.read()
            await asyncio.to_thread(_write_bytes_sync, path, content)
        logger.debug("Downloaded audio file to: %s", path)

    async def solve_captcha(self) -> bool:
        """
        Attempts to solve Google reCAPTCHA v2 (checkbox or audio challenge).
        """
        if self.debug and not self.current_diag_dir:
            await self.save_captcha_diagnostics(reason="solve_captcha_triggered")

        if self.metadata:
            self.metadata.setdefault("solution", {})["attempted"] = True

        try:
            # Wait for the CAPTCHA iframe to be available
            recaptcha_frame = self.page.frame_locator('iframe[title*="reCAPTCHA"]')

            # Click on the CAPTCHA checkbox
            checkbox = recaptcha_frame.locator("#recaptcha-anchor")
            if await checkbox.count() > 0:
                await checkbox.click()
                await asyncio.sleep(1.0)

            # Check if the CAPTCHA is solved directly by checkbox
            if await self.is_solved():
                logger.info("🎉 CAPTCHA solved by checkbox click.")
                if self.metadata:
                    self.metadata["solution"]["solved"] = True
                    self.metadata["solution"]["method"] = "checkbox"
                    await self._save_metadata_file()
                return True

            # If not solved, attempt audio CAPTCHA solving
            return await self.solve_audio_captcha()

        except Exception as e:
            logger.error("An error occurred while solving CAPTCHA: %s", e)
            if self.metadata:
                self.metadata["solution"]["solved"] = False
                self.metadata["solution"]["error"] = str(e)
                await self._save_metadata_file()
            raise

    async def solve_audio_captcha(self) -> bool:
        """Solves reCAPTCHA v2 audio challenge using speech recognition."""
        if self.debug:
            work_dir = self._get_diag_dir()
            return await self._process_audio_challenge(work_dir)

        # In standard mode, use a temporary directory cleaned up automatically
        with tempfile.TemporaryDirectory(prefix="captcha_audio_") as tmp_dir:
            return await self._process_audio_challenge(tmp_dir)

    async def _process_audio_challenge(self, target_dir: str) -> bool:
        """Internal worker to process the audio challenge inside target_dir."""
        try:
            # Switch to the audio CAPTCHA iframe
            challenge_frame = self.page.frame_locator(
                'iframe[title*="recaptcha challenge expires in two minutes"]'
            )

            # Screenshot challenge frame if in debug mode
            if self.debug:
                try:
                    challenge_el = self.page.locator(
                        'iframe[title*="recaptcha challenge expires in two minutes"]'
                    )
                    if (
                        await challenge_el.count() > 0
                        and await challenge_el.first.is_visible()
                    ):
                        chal_path = os.path.join(target_dir, "challenge.png")
                        await challenge_el.first.screenshot(path=chal_path)
                        if self.metadata:
                            self.metadata.setdefault("files", {})["challenge"] = (
                                "challenge.png"
                            )
                except (PlaywrightError, OSError) as e:
                    logger.debug("Failed to screenshot challenge iframe: %s", e)

            # Click on the audio challenge button
            await challenge_frame.locator("#recaptcha-audio-button").click()
            await asyncio.sleep(1.0)

            # Get the audio source URL
            audio_source = await challenge_frame.locator("#audio-source").get_attribute(
                "src"
            )
            logger.info("Audio challenge source URL: %s", audio_source)

            path_to_mp3 = os.path.join(target_dir, "audio.mp3")
            path_to_wav = os.path.join(target_dir, "audio.wav")

            if audio_source:
                if self.metadata:
                    self.metadata.setdefault("audio", {})["source_url"] = audio_source

                await self.download_audio(audio_source, path_to_mp3)

                if self.metadata:
                    self.metadata["audio"]["mp3"] = "audio.mp3"
                    self.metadata.setdefault("files", {})["audio_mp3"] = "audio.mp3"

                # Convert mp3 to wav off the event loop
                def _convert():
                    sound = AudioSegment.from_mp3(path_to_mp3)
                    sound.export(path_to_wav, format="wav")

                await asyncio.to_thread(_convert)

                if self.metadata:
                    self.metadata["audio"]["wav"] = "audio.wav"
                    self.metadata.setdefault("files", {})["audio_wav"] = "audio.wav"
                logger.debug("Converted MP3 to WAV.")

                # Recognize the audio off the event loop
                recognizer: Any = sr.Recognizer()

                def _transcribe():
                    with sr.AudioFile(path_to_wav) as source:
                        audio = recognizer.record(source)
                    return recognizer.recognize_google(audio).lower()

                captcha_text = await asyncio.to_thread(_transcribe)
                logger.info("Recognized CAPTCHA text: %s", captcha_text)

                if self.metadata:
                    self.metadata["audio"]["recognized_text"] = captcha_text

                # Enter the CAPTCHA text
                await challenge_frame.locator("#audio-response").fill(captcha_text)
                await challenge_frame.locator("#audio-response").press("Enter")
                logger.debug("Entered and submitted CAPTCHA text.")

                # Wait for CAPTCHA to be processed
                await asyncio.sleep(1.0)

            # Verify CAPTCHA is solved
            if await self.is_solved():
                logger.info("🎉 Audio CAPTCHA solved successfully.")
                if self.metadata:
                    self.metadata["solution"]["solved"] = True
                    self.metadata["solution"]["method"] = "audio"
                    await self._save_metadata_file()
                return True

            logger.warning("❌ Failed to solve audio CAPTCHA.")
            if self.metadata:
                self.metadata["solution"]["solved"] = False
                self.metadata["solution"]["method"] = "audio"
                self.metadata["solution"]["error"] = (
                    "Verification failed after audio submit"
                )
                await self._save_metadata_file()
            raise RuntimeError("Failed to solve CAPTCHA")

        except Exception as e:
            logger.error("An error occurred while solving audio CAPTCHA: %s", e)
            if self.metadata:
                self.metadata["solution"]["solved"] = False
                self.metadata["solution"]["error"] = str(e)
                await self._save_metadata_file()
            raise

    async def is_solved(self) -> bool:
        """Checks if the reCAPTCHA checkbox is marked as checked."""
        try:
            recaptcha_frame = self.page.frame_locator('iframe[title*="reCAPTCHA"]')
            checkbox = recaptcha_frame.locator("#recaptcha-anchor")
            if await checkbox.count() == 0:
                return False

            aria_checked = await checkbox.get_attribute("aria-checked")
            checkbox_class = await checkbox.get_attribute("class")

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
