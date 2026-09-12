import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import random
import re
import time
from typing import Any

import aiohttp
from pydub import AudioSegment
import speech_recognition as sr

logger = logging.getLogger("root.recaptcha_solver")


class RecaptchaSolver:
    def __init__(self, page, debug_dir: str = "debug/captchas"):
        self.page = page
        self.debug_dir = debug_dir
        self.current_diag_dir: str | None = None
        self.metadata: dict[str, Any] = {}

    def _get_diag_dir(self) -> str:
        if not self.current_diag_dir:
            now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            rand_suffix = random.randint(1000, 9999)
            self.current_diag_dir = os.path.join(
                self.debug_dir, f"captcha_{now_str}_{rand_suffix}"
            )
            os.makedirs(self.current_diag_dir, exist_ok=True)
        return self.current_diag_dir

    def _save_metadata_file(self):
        if not self.current_diag_dir:
            return
        meta_path = os.path.join(self.current_diag_dir, "meta.json")
        try:
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(self.metadata, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.debug(f"Error writing meta.json: {e}")

    async def save_captcha_diagnostics(self, reason: str = "detected") -> str:
        """
        Saves all necessary diagnostic information when a CAPTCHA or Sorry page is encountered:
        - Full page screenshot (screenshot.png)
        - Raw HTML source (page.html)
        - Metadata JSON (meta.json) containing sitekey, data-s, cookies, user-agent, form fields, IP, timestamp.
        """
        diag_dir = self._get_diag_dir()
        logger.info(f"💾 Saving CAPTCHA diagnostic data into: {diag_dir}")

        # 1. Capture full-page screenshot
        screenshot_path = os.path.join(diag_dir, "screenshot.png")
        try:
            await self.page.screenshot(path=screenshot_path, full_page=True)
        except Exception as e:
            logger.debug(f"Failed to capture screenshot: {e}")

        # 2. Capture complete HTML source
        html_content = ""
        html_path = os.path.join(diag_dir, "page.html")
        try:
            html_content = await self.page.content()
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html_content)
        except Exception as e:
            logger.debug(f"Failed to capture HTML content: {e}")

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
                m_k = re.search(r'data-sitekey=["\']([a-zA-Z0-9_-]+)["\']', html_content)
                if m_k:
                    sitekey = m_k.group(1)

            if not data_s:
                m_s = re.search(r'data-s=["\']([a-zA-Z0-9_-]+)["\']', html_content)
                if m_s:
                    data_s = m_s.group(1)
        except Exception:
            pass

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
        except Exception:
            pass

        # 6. Extract cookies, User-Agent & viewport
        cookies = []
        try:
            cookies = await self.page.context.cookies()
        except Exception:
            pass

        user_agent = ""
        try:
            user_agent = await self.page.evaluate("() => navigator.userAgent")
        except Exception:
            pass

        # 7. Build metadata dictionary
        self.metadata = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
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

        self._save_metadata_file()
        logger.info(
            f"✅ Saved CAPTCHA diagnostic info: sitekey={sitekey}, data_s={'yes' if data_s else 'no'}, ip={ip_address}"
        )
        return diag_dir

    async def download_audio(self, url, path):
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                with open(path, "wb") as f:
                    f.write(await response.read())
        logger.debug("Downloaded audio asynchronously.")

    async def solveCaptcha(self) -> bool:
        """
        Attempts to solve Google reCAPTCHA v2 (checkbox or audio challenge),
        automatically capturing all diagnostic data.
        """
        if not self.current_diag_dir:
            await self.save_captcha_diagnostics(reason="solve_captcha_triggered")

        self.metadata["solution"]["attempted"] = True

        try:
            # Wait for the CAPTCHA iframe to be available
            recaptcha_frame = self.page.frame_locator('iframe[title*="reCAPTCHA"]')

            # Click on the CAPTCHA checkbox
            checkbox = recaptcha_frame.locator("#recaptcha-anchor")
            if await checkbox.count() > 0:
                await checkbox.click()
                await asyncio.sleep(1.0)

            # Check if the CAPTCHA is solved directly by checkbox
            if await self.isSolved():
                logger.info("🎉 CAPTCHA solved by checkbox click.")
                self.metadata["solution"]["solved"] = True
                self.metadata["solution"]["method"] = "checkbox"
                self._save_metadata_file()
                return True

            # If not solved, attempt audio CAPTCHA solving
            solved = await self.solveAudioCaptcha()
            return solved

        except Exception as e:
            logger.error(f"An error occurred while solving CAPTCHA: {e}")
            self.metadata["solution"]["solved"] = False
            self.metadata["solution"]["error"] = str(e)
            self._save_metadata_file()
            raise

    async def solveAudioCaptcha(self) -> bool:
        diag_dir = self._get_diag_dir()
        try:
            # Switch to the audio CAPTCHA iframe
            challenge_frame = self.page.frame_locator(
                'iframe[title*="recaptcha challenge expires in two minutes"]'
            )

            # Take screenshot of challenge frame if visible
            try:
                challenge_el = self.page.locator(
                    'iframe[title*="recaptcha challenge expires in two minutes"]'
                )
                if await challenge_el.count() > 0 and await challenge_el.first.is_visible():
                    await challenge_el.first.screenshot(
                        path=os.path.join(diag_dir, "challenge.png")
                    )
                    self.metadata["files"]["challenge"] = "challenge.png"
            except Exception:
                pass

            # Click on the audio button
            await challenge_frame.locator("#recaptcha-audio-button").click()
            await asyncio.sleep(1.0)

            # Get the audio source URL
            audio_source = await challenge_frame.locator("#audio-source").get_attribute(
                "src"
            )
            logger.info(f"Audio challenge source URL: {audio_source}")

            path_to_mp3 = os.path.join(diag_dir, "audio.mp3")
            path_to_wav = os.path.join(diag_dir, "audio.wav")

            if audio_source:
                self.metadata["audio"]["source_url"] = audio_source
                await self.download_audio(audio_source, path_to_mp3)
                self.metadata["audio"]["mp3"] = "audio.mp3"
                self.metadata["files"]["audio_mp3"] = "audio.mp3"

                # Convert mp3 to wav
                sound = AudioSegment.from_mp3(path_to_mp3)
                sound.export(path_to_wav, format="wav")
                self.metadata["audio"]["wav"] = "audio.wav"
                self.metadata["files"]["audio_wav"] = "audio.wav"
                logger.debug("Converted MP3 to WAV.")

                # Recognize the audio
                recognizer: Any = sr.Recognizer()
                with sr.AudioFile(path_to_wav) as source:
                    audio = recognizer.record(source)
                captcha_text = recognizer.recognize_google(audio).lower()
                logger.info(f"Recognized CAPTCHA text: {captcha_text}")
                self.metadata["audio"]["recognized_text"] = captcha_text

                # Enter the CAPTCHA text
                await challenge_frame.locator("#audio-response").fill(captcha_text)
                await challenge_frame.locator("#audio-response").press("Enter")
                logger.debug("Entered and submitted CAPTCHA text.")

                # Wait for CAPTCHA to be processed
                await asyncio.sleep(1.0)

            # Verify CAPTCHA is solved
            if await self.isSolved():
                logger.info("🎉 Audio CAPTCHA solved successfully.")
                self.metadata["solution"]["solved"] = True
                self.metadata["solution"]["method"] = "audio"
                self._save_metadata_file()
                return True
            else:
                logger.warning("❌ Failed to solve audio CAPTCHA.")
                self.metadata["solution"]["solved"] = False
                self.metadata["solution"]["method"] = "audio"
                self.metadata["solution"]["error"] = (
                    "Verification failed after audio submit"
                )
                self._save_metadata_file()
                raise RuntimeError("Failed to solve CAPTCHA")

        except Exception as e:
            logger.error(f"An error occurred while solving audio CAPTCHA: {e}")
            self.metadata["solution"]["solved"] = False
            self.metadata["solution"]["error"] = str(e)
            self._save_metadata_file()
            raise

    async def isSolved(self) -> bool:
        try:
            # Access the reCAPTCHA iframe
            recaptcha_frame = self.page.frame_locator('iframe[title*="reCAPTCHA"]')
            checkbox = recaptcha_frame.locator("#recaptcha-anchor")
            if await checkbox.count() == 0:
                return False

            aria_checked = await checkbox.get_attribute("aria-checked")
            checkbox_class = await checkbox.get_attribute("class")

            return aria_checked == "true" or "recaptcha-checkbox-checked" in (
                checkbox_class or ""
            )
        except Exception as e:
            logger.debug(f"Error checking if CAPTCHA is solved: {e}")
            return False
