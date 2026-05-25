"""
Grok Real-time Voice Agent — speak into mic, Grok speaks back.

Usage:
    pip install websockets pyaudio
    export XAI_API_KEY=your-key-here
    python test_grok_voice.py            # default voice: ara
    python test_grok_voice.py eve        # use eve voice
"""
import asyncio
import json
import os
import base64
import time
import sys

import websockets
import pyaudio

XAI_API_KEY = os.getenv("XAI_API_KEY", "")
SAMPLE_RATE = 24000
CHUNK_SIZE = int(SAMPLE_RATE * 0.1)  # 100ms chunks

VOICES = {"eve": "Female, energetic", "ara": "Female, warm", "rex": "Male, confident", "sal": "Neutral, balanced", "leo": "Male, authoritative"}


async def run(voice="ara"):
    if not XAI_API_KEY:
        print("Set XAI_API_KEY"); return

    pa = pyaudio.PyAudio()
    mic = pa.open(format=pyaudio.paInt16, channels=1, rate=SAMPLE_RATE, input=True, frames_per_buffer=CHUNK_SIZE)
    spk = pa.open(format=pyaudio.paInt16, channels=1, rate=SAMPLE_RATE, output=True, frames_per_buffer=CHUNK_SIZE)

    print(f"Voice: {voice} ({VOICES.get(voice,'')})")
    print("Connecting...")

    async with websockets.connect(
        "wss://api.x.ai/v1/realtime?model=grok-voice-latest",
        additional_headers={"Authorization": f"Bearer {XAI_API_KEY}"}
    ) as ws:

        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "voice": voice,
                "instructions": "You are Ranjitha, a senior VLSI physical design engineer conducting a mock interview. Speak naturally in 1-2 sentences.",
                "turn_detection": {"type": "server_vad", "silence_duration_ms": 1500, "threshold": 0.5},
                "audio": {
                    "input": {"format": {"type": "audio/pcm", "rate": SAMPLE_RATE}},
                    "output": {"format": {"type": "audio/pcm", "rate": SAMPLE_RATE}}
                }
            }
        }))

        await ws.recv()
        print("\n--- SPEAK NOW (Ctrl+C to stop) ---\n")

        t0 = 0

        async def send_mic():
            while True:
                data = mic.read(CHUNK_SIZE, exception_on_overflow=False)
                await ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(data).decode()}))
                await asyncio.sleep(0.05)

        async def receive():
            nonlocal t0
            first = True
            while True:
                event = json.loads(await ws.recv())
                t = event.get("type", "")

                if t == "input_audio_buffer.speech_started":
                    print("  [You speaking...]")

                elif t == "input_audio_buffer.speech_stopped":
                    t0 = time.time()
                    print("  [Thinking...]")

                elif t == "response.output_audio.delta":
                    spk.write(base64.b64decode(event["delta"]))
                    if first:
                        first = False
                        print(f"  [Ranjitha speaking... first audio: {round((time.time()-t0)*1000)}ms]")

                elif t == "response.done":
                    print(f"  [Done: {round((time.time()-t0)*1000)}ms]\n--- YOUR TURN ---\n")
                    first = True

                elif t == "error":
                    print(f"  [ERROR] {event}")

        try:
            await asyncio.gather(send_mic(), receive())
        except KeyboardInterrupt:
            print("\n--- Ended ---")
        finally:
            mic.close(); spk.close(); pa.terminate()


if __name__ == "__main__":
    voice = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in VOICES else "ara"
    asyncio.run(run(voice))
