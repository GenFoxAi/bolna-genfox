import aiohttp
import asyncio
import os
import websockets
from websockets.exceptions import InvalidHandshake, ConnectionClosed
import copy
import time
import uuid
import traceback
import json
import base64
from collections import deque

from .base_synthesizer import BaseSynthesizer
from bolna.helpers.logger_config import configure_logger
from bolna.helpers.utils import (
    create_ws_data_packet,
    get_synth_audio_format,
    resample,
    wav_bytes_to_pcm,
)
from bolna.constants import SARVAM_MODEL_SAMPLING_RATE_MAPPING

logger = configure_logger(__name__)


class SarvamSynthesizer(BaseSynthesizer):
    """
    Sarvam TTS supports:
      - HTTP: https://api.sarvam.ai/text-to-speech
      - WS streaming: wss://api.sarvam.ai/text-to-speech/ws?model=...&send_completion_event=true

    This implementation:
      - Keeps a single WS connection per call
      - Reconnects automatically
      - Associates each "flush" with one meta_info (even if multiple audio frames arrive)
      - Emits a single b'\\x00' sentinel ONLY for the final LLM chunk of a response
    """

    def __init__(
        self,
        voice_id,
        model,
        language,
        sampling_rate="8000",
        stream=False,
        buffer_size=400,
        speed=1.0,
        synthesizer_key=None,
        temperature=None,
        output_audio_codec="wav",
        output_audio_bitrate="32k",
        max_chunk_length=250,
        **kwargs,
    ):
        super().__init__(kwargs.get("task_manager_instance", None), stream)

        self.api_key = os.environ.get("SARVAM_API_KEY") if synthesizer_key is None else synthesizer_key
        if not self.api_key:
            raise ValueError("SARVAM_API_KEY is missing (env var) and synthesizer_key not provided")

        self.voice_id = voice_id
        self.model = model
        self.language = language

        self.stream = stream
        self.sampling_rate = int(sampling_rate)

        # Use mapping if known; otherwise we’ll infer from audio headers.
        self.original_sampling_rate = SARVAM_MODEL_SAMPLING_RATE_MAPPING.get(model)

        self.api_url = "https://api.sarvam.ai/text-to-speech"
        # IMPORTANT: send_completion_event=true gives us final events on WS
        self.ws_url = f"wss://api.sarvam.ai/text-to-speech/ws?model={model}&send_completion_event=true"

        # Tuning knobs
        self.loudness = 1.0
        self.pitch = 0.0
        self.pace = float(speed)
        self.temperature = temperature  # only used for some models (e.g. bulbul:v3)
        self.enable_preprocessing = True

        self.output_audio_codec = output_audio_codec
        self.output_audio_bitrate = output_audio_bitrate
        self.max_chunk_length = int(max_chunk_length)

        self.buffer_size = int(buffer_size)
        # Sarvam expects min_buffer_size in a sane range; keep guardrails.
        if self.buffer_size < 30:
            self.buffer_size = 30
        if self.buffer_size > 200:
            self.buffer_size = 200

        # Streaming state
        self.connection_time = None
        self.turn_latencies = []
        self.conversation_ended = False

        self.websocket_holder = {"websocket": None}
        self._ws_lock = asyncio.Lock()

        # Each push adds one meta_info for one flush text chunk
        self._meta_queue = deque()

        # Active flush meta for potentially multiple audio messages
        self._active_meta = None
        self._active_first_chunk = False

        # Used for “final end-of-stream” signal
        self._active_is_final_llm_chunk = False

        # Task pointers
        self.sender_task = None

        # Debug throttling
        self._last_ws_not_connected_log = 0.0

        # Metrics
        self.synthesized_characters = 0
        self.current_turn_start_time = None
        self.current_turn_id = None

        logger.info(
            f"[sarvam:init] model={self.model} voice={self.voice_id} lang={self.language} "
            f"stream={self.stream} sr={self.sampling_rate} ws_url={self.ws_url}"
        )

    def get_engine(self):
        return self.model

    def supports_websocket(self):
        return True

    def get_sleep_time(self):
        return 0.01

    def get_synthesized_characters(self):
        return self.synthesized_characters

    async def get_sender_task(self):
        return self.sender_task

    # ---------------------------
    # HTTP (non-stream) path
    # ---------------------------
    async def __send_payload(self, payload):
        headers = {"api-subscription-key": self.api_key, "Content-Type": "application/json"}
        async with aiohttp.ClientSession() as session:
            async with session.post(self.api_url, headers=headers, json=payload) as response:
                txt = await response.text()
                if response.status != 200:
                    logger.error(f"[sarvam:http] status={response.status} body={txt}")
                    return None
                try:
                    data = json.loads(txt)
                except Exception:
                    logger.error(f"[sarvam:http] invalid json body={txt}")
                    return None

                audios = data.get("audios", [])
                if isinstance(audios, list) and audios:
                    return audios[0]
                logger.error(f"[sarvam:http] missing audios in response: keys={list(data.keys())}")
                return None

    def form_payload(self, text):
        # Use the native model sampling rate for the API request.
        # bulbul:v3 only supports 22050 Hz; sending 8000 causes a 400 validation error.
        # We always resample the returned audio ourselves to the target sampling_rate.
        api_sample_rate = self.original_sampling_rate or self.sampling_rate

        payload = {
            "target_language_code": self.language,
            "text": text,
            "speaker": self.voice_id,
            "speech_sample_rate": api_sample_rate,
            "model": self.model,
            "enable_preprocessing": self.enable_preprocessing,
            "output_audio_codec": self.output_audio_codec,
        }

        # Only include bitrate for compressed codecs (not WAV which is uncompressed PCM)
        if self.output_audio_codec != "wav":
            payload["output_audio_bitrate"] = self.output_audio_bitrate

        # bulbul:v3 family differences: does NOT support pitch/loudness/pace/enable_preprocessing
        if self.model.startswith("bulbul:v3"):
            payload.pop("enable_preprocessing", None)
            payload.pop("output_audio_bitrate", None)  # strip even if set above
            if self.temperature is not None:
                payload["temperature"] = self.temperature
        else:
            payload["pitch"] = self.pitch
            payload["loudness"] = self.loudness
            payload["pace"] = self.pace

        return payload

    async def synthesize(self, text):
        # Non-stream mode uses HTTP and returns base64 audio string (as Sarvam returns)
        return await self.__send_payload(self.form_payload(text))

    # ---------------------------
    # WS (stream) path
    # ---------------------------
    def _build_ws_config(self):
        """
        Keep config strictly compatible with Sarvam streaming API.
        (Avoid unsupported keys for bulbul:v3.)
        """
        # bulbul:v3 native rate is 22050Hz; sending 8000 causes 422 errors.
        # Resampling happens in _process_audio_data after we receive the audio.
        api_sample_rate = self.original_sampling_rate or self.sampling_rate

        cfg = {
            "target_language_code": self.language,
            "speaker": self.voice_id,
            "speech_sample_rate": api_sample_rate,
            "output_audio_codec": self.output_audio_codec,
            "max_chunk_length": self.max_chunk_length,
            "min_buffer_size": self.buffer_size,
        }

        # Only include bitrate for compressed codecs
        if self.output_audio_codec != "wav":
            cfg["output_audio_bitrate"] = self.output_audio_bitrate

        if self.model.startswith("bulbul:v3"):
            # v3 doesn't support pitch/loudness/pace/min_buffer_size/max_chunk_length
            cfg.pop("max_chunk_length", None)
            cfg.pop("min_buffer_size", None)
            cfg.pop("output_audio_bitrate", None)  # not valid for v3
            # pace is not supported by bulbul:v3
            if self.temperature is not None:
                cfg["temperature"] = self.temperature
        else:
            cfg["pitch"] = self.pitch
            cfg["loudness"] = self.loudness
            cfg["pace"] = self.pace
            cfg["enable_preprocessing"] = self.enable_preprocessing

        return cfg

    async def _connect_ws(self):
        """
        Establish WS connection + send config.
        Uses extra_headers for compatibility with current websockets versions.
        """
        headers = [("api-subscription-key", self.api_key)]
        cfg = self._build_ws_config()

        logger.info(f"[sarvam:ws] connect attempt url={self.ws_url}")
        logger.info(f"[sarvam:ws] config => {cfg}")

        start = time.perf_counter()
        try:
            # websockets.connect expects extra_headers in modern versions
            ws = await websockets.connect(
                self.ws_url,
                extra_headers=headers,
                open_timeout=10,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
                max_size=None,
            )
        except TypeError as e:
            # Some older code used `additional_headers` — fallback for older libs
            logger.warning(f"[sarvam:ws] websockets.connect TypeError, retrying w/ additional_headers: {e}")
            ws = await websockets.connect(
                self.ws_url,
                additional_headers={"api-subscription-key": self.api_key},
            )
        except InvalidHandshake as e:
            logger.error(f"[sarvam:ws] handshake failed: {e}", exc_info=True)
            return None
        except Exception as e:
            logger.error(f"[sarvam:ws] connect failed: {e}", exc_info=True)
            return None

        try:
            await ws.send(json.dumps({"type": "config", "data": cfg}))
        except Exception as e:
            logger.error(f"[sarvam:ws] failed to send config: {e}", exc_info=True)
            try:
                await ws.close()
            except Exception:
                pass
            return None

        if self.connection_time is None:
            self.connection_time = round((time.perf_counter() - start) * 1000)

        logger.info(f"[sarvam:ws] CONNECTED model={self.model} ({self.connection_time}ms)")
        return ws

    async def ensure_connection(self):
        """
        Ensure websocket_holder['websocket'] is OPEN. Reconnect if needed.
        """
        if self.conversation_ended:
            return None

        ws = self.websocket_holder.get("websocket")
        if ws is not None and ws.state is websockets.protocol.State.OPEN:
            return ws

        async with self._ws_lock:
            # re-check inside lock
            ws = self.websocket_holder.get("websocket")
            if ws is not None and ws.state is websockets.protocol.State.OPEN:
                return ws

            new_ws = await self._connect_ws()
            self.websocket_holder["websocket"] = new_ws
            return new_ws

    async def monitor_connection(self):
        """
        Background task from TaskManager: keeps WS alive/reconnected.
        """
        consecutive_failures = 0
        backoff = 1.0
        while not self.conversation_ended:
            ws = self.websocket_holder.get("websocket")
            if ws is None or ws.state is websockets.protocol.State.CLOSED:
                logger.info("[sarvam:ws] monitor: ws closed/missing, reconnecting...")
                new_ws = await self.ensure_connection()
                if new_ws is None:
                    consecutive_failures += 1
                    logger.warning(f"[sarvam:ws] monitor: reconnect failed attempt={consecutive_failures}")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 8.0)
                else:
                    consecutive_failures = 0
                    backoff = 1.0
            await asyncio.sleep(0.5)

    async def sender(self, text, sequence_id, end_of_llm_stream=False):
        """
        Sends text chunks + flush. Will attempt connection itself (no infinite waiting).
        """
        try:
            if self.conversation_ended:
                logger.info("[sarvam:sender] conversation ended, skipping send")
                return

            if not self.should_synthesize_response(sequence_id):
                logger.info(f"[sarvam:sender] skip invalid sequence_id={sequence_id}")
                return

            ws = await self.ensure_connection()
            if ws is None:
                logger.error("[sarvam:sender] unable to connect ws; dropping chunk")
                return

            if text:
                logger.info(f"[sarvam:sender] send text len={len(text)} seq={sequence_id} eos={end_of_llm_stream}")
                await ws.send(json.dumps({"type": "text", "data": {"text": text}}))

            # Always flush after sending (Sarvam starts producing audio)
            await ws.send(json.dumps({"type": "flush"}))

            # Track whether THIS flush corresponds to final LLM chunk
            if end_of_llm_stream:
                # The meta_info already carries end_of_llm_stream; receiver will use it.
                logger.info(f"[sarvam:sender] marked final llm chunk for seq={sequence_id}")

        except ConnectionClosed as e:
            logger.error(f"[sarvam:sender] ws closed during send: {e}", exc_info=True)
            self.websocket_holder["websocket"] = None
        except Exception as e:
            logger.error(f"[sarvam:sender] unexpected error: {e}", exc_info=True)

    async def receiver(self):
        """
        Receives server messages.
        Yields:
          - raw audio bytes for "audio" frames
          - b'\\x00' ONLY when completion event arrives AND active chunk is final LLM chunk
        """
        while not self.conversation_ended:
            ws = self.websocket_holder.get("websocket")

            if ws is None or ws.state is websockets.protocol.State.CLOSED:
                now = time.time()
                if now - self._last_ws_not_connected_log > 2.0:
                    logger.info("[sarvam:receiver] ws not connected; attempting ensure_connection...")
                    self._last_ws_not_connected_log = now
                await self.ensure_connection()
                await asyncio.sleep(0.05)
                continue

            try:
                raw = await ws.recv()
            except ConnectionClosed:
                logger.warning("[sarvam:receiver] ws closed; will reconnect")
                self.websocket_holder["websocket"] = None
                self._active_meta = None
                self._active_first_chunk = False
                await asyncio.sleep(0.05)
                continue
            except Exception as e:
                logger.error(f"[sarvam:receiver] recv error: {e}", exc_info=True)
                self.websocket_holder["websocket"] = None
                await asyncio.sleep(0.1)
                continue

            try:
                data = json.loads(raw)
            except Exception:
                logger.warning(f"[sarvam:receiver] non-json message: {raw!r}")
                continue

            msg_type = data.get("type")

            if msg_type == "audio":
                b64_audio = (data.get("data") or {}).get("audio")
                if not b64_audio:
                    logger.warning(f"[sarvam:receiver] audio frame missing audio field: {data}")
                    continue
                try:
                    yield base64.b64decode(b64_audio)
                except Exception as e:
                    logger.error(f"[sarvam:receiver] base64 decode failed: {e}", exc_info=True)
                    continue

            elif msg_type in ("event", "completion_event"):
                # Official docs refer to completion events when enabled.
                event_type = (data.get("data") or {}).get("event_type") or (data.get("data") or {}).get("type")
                logger.info(f"[sarvam:receiver] event received event_type={event_type} data={data.get('data')}")

                # Only treat as "end of stream" if the active meta indicates end_of_llm_stream
                if event_type == "final":
                    if self._active_meta and self._active_meta.get("end_of_llm_stream", False):
                        yield b"\x00"

                    # Reset for next flush
                    self._active_meta = None
                    self._active_first_chunk = False

            elif msg_type == "error":
                logger.error(f"[sarvam:receiver] server error: {data}")
                # Force reconnect on server error
                try:
                    await ws.close()
                except Exception:
                    pass
                self.websocket_holder["websocket"] = None
                self._active_meta = None
                self._active_first_chunk = False

            else:
                logger.info(f"[sarvam:receiver] other message type={msg_type} data={data}")

    async def _process_audio_data(self, audio_bytes):
        """
        Converts Sarvam output to PCM at target sampling_rate.
        """
        fmt = get_synth_audio_format(audio_bytes)

        # Try to infer original sampling rate if unknown and WAV
        if fmt == "wav" and not self.original_sampling_rate and len(audio_bytes) >= 28:
            try:
                self.original_sampling_rate = int.from_bytes(audio_bytes[24:28], byteorder="little")
                logger.info(f"[sarvam:audio] inferred original_sampling_rate={self.original_sampling_rate}")
            except Exception:
                pass

        try:
            resampled = resample(
                audio_bytes,
                int(self.sampling_rate),
                format=fmt,
                original_sample_rate=self.original_sampling_rate,
            )
        except Exception as e:
            logger.error(f"[sarvam:audio] resample failed: {e}", exc_info=True)
            return None

        if fmt == "wav":
            try:
                return wav_bytes_to_pcm(resampled)
            except Exception as e:
                logger.error(f"[sarvam:audio] wav->pcm failed: {e}", exc_info=True)
                return None
        return resampled

    async def generate(self):
        """
        Wrap receiver() into Bolna WS data packets with correct meta_info + markers.
        """
        try:
            if not self.stream:
                return

            async for raw_audio in self.receiver():
                if self.conversation_ended:
                    return

                # Active meta assignment: pop only when first audio arrives for a flush
                if self._active_meta is None and len(self._meta_queue) > 0 and raw_audio != b"\x00":
                    self._active_meta = self._meta_queue.popleft()
                    self._active_first_chunk = True

                    # latency tracking
                    try:
                        if self.current_turn_start_time is not None:
                            first_latency = time.perf_counter() - self.current_turn_start_time
                            self._active_meta["synthesizer_latency"] = first_latency
                    except Exception:
                        pass

                meta = self._active_meta or {}

                # Completion sentinel (internal only)
                if raw_audio == b"\x00":
                    # mark end only if this was final llm chunk
                    if meta.get("end_of_llm_stream", False):
                        meta["end_of_synthesizer_stream"] = True
                    meta["is_first_chunk"] = False
                    meta["format"] = "pcm"
                    meta["mark_id"] = str(uuid.uuid4())
                    yield create_ws_data_packet(raw_audio, meta)
                    continue

                processed = await self._process_audio_data(raw_audio)
                if processed is None:
                    continue

                meta["format"] = "pcm"
                meta["is_first_chunk"] = bool(self._active_first_chunk)
                self._active_first_chunk = False

                meta["mark_id"] = str(uuid.uuid4())
                yield create_ws_data_packet(processed, meta)

        except asyncio.CancelledError:
            logger.info("[sarvam:generate] cancelled")
        except Exception as e:
            traceback.print_exc()
            logger.error(f"[sarvam:generate] error: {e}", exc_info=True)

    async def push(self, message):
        """
        Receives text chunks from TaskManager and schedules sending over WS.
        """
        if not self.stream:
            # Non-stream path uses HTTP via TaskManager’s non-stream flow
            self.internal_queue.put_nowait(message)
            return

        meta_info = copy.deepcopy(message.get("meta_info", {}))
        text = message.get("data") or ""

        self.synthesized_characters += len(text)

        end_of_llm_stream = bool(meta_info.get("end_of_llm_stream", False))

        self.current_turn_start_time = time.perf_counter()
        self.current_turn_id = meta_info.get("turn_id") or meta_info.get("sequence_id")

        # Track meta per flush
        meta_info["text"] = text
        self._meta_queue.append(meta_info)

        # Start sender task
        self.sender_task = asyncio.create_task(
            self.sender(text, meta_info.get("sequence_id"), end_of_llm_stream=end_of_llm_stream)
        )

    async def handle_interruption(self):
        """
        Called by TaskManager on interruption. Best effort cancel: close WS + clear queues.
        """
        logger.info("[sarvam] handle_interruption: closing ws and clearing queued meta")
        self._meta_queue.clear()
        self._active_meta = None
        self._active_first_chunk = False
        ws = self.websocket_holder.get("websocket")
        try:
            if ws is not None:
                await ws.close()
        except Exception:
            pass
        self.websocket_holder["websocket"] = None

    async def flush_synthesizer_stream(self):
        """
        TaskManager calls this after interruption; safest is to close and reconnect on demand.
        """
        await self.handle_interruption()

    async def cleanup(self):
        self.conversation_ended = True
        logger.info("[sarvam:cleanup] cleaning sarvam synthesizer tasks")

        if self.sender_task:
            try:
                self.sender_task.cancel()
                await self.sender_task
            except asyncio.CancelledError:
                logger.info("[sarvam:cleanup] sender task cancelled")

        ws = self.websocket_holder.get("websocket")
        try:
            if ws is not None:
                await ws.close()
        except Exception:
            pass

        self.websocket_holder["websocket"] = None
        logger.info("[sarvam:cleanup] ws closed")