"""
Grok Voice Agent Test — Real-time bidirectional voice conversation.

This is the REAL voice agent: you speak into mic, Grok listens + thinks + speaks back.
No separate STT/LLM/TTS — everything happens in one WebSocket stream.

Usage:
    pip install websockets pyaudio
    export XAI_API_KEY=your-key-here
    python test_grok_voice.py          # test with voice "ara"
    python test_grok_voice.py eve      # test with voice "eve"
    python test_grok_voice.py text     # text-only mode (no mic needed)
"""
import asyncio
import json
import os
import base64
import wave
import time
import sys
import struct

import websockets

XAI_API_KEY = os.getenv("XAI_API_KEY", "")
SAMPLE_RATE = 24000
CHANNELS = 1
CHUNK_DURATION_MS = 100  # send audio every 100ms

VOICES = {"eve": "Female, energetic", "ara": "Female, warm", "rex": "Male, confident", "sal": "Neutral, balanced", "leo": "Male, authoritative"}


async def test_voice_agent_realtime(voice="ara"):
    """Full real-time voice agent — mic input, audio output."""
    try:
        import pyaudio
    except ImportError:
        print("ERROR: pip install pyaudio")
        return

    if not XAI_API_KEY:
        print("ERROR: Set XAI_API_KEY environment variable")
        return

    print(f"\n{'='*50}")
    print(f"GROK VOICE AGENT — Real-time Test")
    print(f"Voice: {voice} ({VOICES.get(voice, '')})")
    print(f"{'='*50}")
    print("[1] Connecting...")

    pa = pyaudio.PyAudio()
    audio_out_chunks = []
    speaking = False
    t0_response = 0
    first_audio_ms = 0

    async with websockets.connect(
        "wss://api.x.ai/v1/realtime?model=grok-voice-latest",
        additional_headers={"Authorization": f"Bearer {XAI_API_KEY}"}
    ) as ws:
        print("[2] Connected. Configuring session...")

        # Configure with server-side VAD (auto-detects when you stop talking)
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "voice": voice,
                "instructions": "You are Ranjitha, a senior VLSI physical design engineer. You are conducting a technical mock interview. Speak naturally in 1-2 sentences. React to the candidate's answer, then ask one follow-up question.",
                "turn_detection": {
                    "type": "server_vad",
                    "silence_duration_ms": 1500,
                    "threshold": 0.5
                },
                "audio": {
                    "input": {"format": {"type": "audio/pcm", "rate": SAMPLE_RATE}},
                    "output": {"format": {"type": "audio/pcm", "rate": SAMPLE_RATE}}
                }
            }
        }))

        # Wait for session created
        msg = await ws.recv()
        event = json.loads(msg)
        print(f"[3] {event['type']}")

        # Open mic input stream
        chunk_size = int(SAMPLE_RATE * CHUNK_DURATION_MS / 1000)
        mic_stream = pa.open(format=pyaudio.paInt16, channels=CHANNELS, rate=SAMPLE_RATE,
                             input=True, frames_per_buffer=chunk_size)

        # Open speaker output stream
        speaker_stream = pa.open(format=pyaudio.paInt16, channels=CHANNELS, rate=SAMPLE_RATE,
                                  output=True, frames_per_buffer=chunk_size)

        print("\n--- SPEAK NOW (Ctrl+C to stop) ---\n")

        # Task: send mic audio to Grok
        async def send_mic_audio():
            while True:
                try:
                    audio_data = mic_stream.read(chunk_size, exception_on_overflow=False)
                    b64 = base64.b64encode(audio_data).decode("utf-8")
                    await ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": b64
                    }))
                    await asyncio.sleep(CHUNK_DURATION_MS / 1000)
                except Exception:
                    break

        # Task: receive events from Grok
        async def receive_events():
            nonlocal speaking, t0_response, first_audio_ms
            full_text = ""

            async for msg in ws:
                event = json.loads(msg)
                etype = event.get("type", "")

                if etype == "response.output_audio.delta":
                    # Play audio immediately
                    chunk = base64.b64decode(event["delta"])
                    speaker_stream.write(chunk)

                    if not speaking:
                        speaking = True
                        first_audio_ms = round((time.time() - t0_response) * 1000) if t0_response else 0
                        print(f"  [Ranjitha speaking... first audio: {first_audio_ms}ms]")

                elif etype == "response.text.delta":
                    text = event.get("delta", "")
                    full_text += text
                    print(text, end="", flush=True)

                elif etype == "response.done":
                    speaking = False
                    total_ms = round((time.time() - t0_response) * 1000) if t0_response else 0
                    print(f"\n  [Response done: {total_ms}ms total, first audio: {first_audio_ms}ms]")
                    print("\n--- YOUR TURN (speak now) ---\n")
                    full_text = ""

                elif etype == "input_audio_buffer.speech_started":
                    print("  [You started speaking...]")

                elif etype == "input_audio_buffer.speech_stopped":
                    t0_response = time.time()
                    print("  [You stopped. Grok thinking...]")

                elif etype == "error":
                    print(f"\n  [ERROR] {json.dumps(event, indent=2)}")
                    break

        # Run both tasks
        try:
            await asyncio.gather(send_mic_audio(), receive_events())
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\n\n--- Session ended ---")
        finally:
            mic_stream.stop_stream()
            mic_stream.close()
            speaker_stream.stop_stream()
            speaker_stream.close()
            pa.terminate()


async def test_text_mode(voice="ara"):
    """Text-only mode — no mic needed. Good for quick testing."""
    if not XAI_API_KEY:
        print("ERROR: Set XAI_API_KEY environment variable")
        return

    print(f"\n{'='*50}")
    print(f"GROK VOICE AGENT — Text Mode")
    print(f"Voice: {voice} ({VOICES.get(voice, '')})")
    print(f"{'='*50}")

    async with websockets.connect(
        "wss://api.x.ai/v1/realtime?model=grok-voice-latest",
        additional_headers={"Authorization": f"Bearer {XAI_API_KEY}"}
    ) as ws:
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "voice": voice,
                "instructions": "You are Ranjitha, a senior VLSI physical design engineer conducting a mock interview. Greet the candidate and ask your first question.",
                "turn_detection": None,
                "audio": {
                    "output": {"format": {"type": "audio/pcm", "rate": SAMPLE_RATE}}
                }
            }
        }))

        msg = await ws.recv()
        print(f"Session: {json.loads(msg)['type']}")

        messages = [
            "Hi, my name is Rahul. I have 2 years experience in physical design using ICC2.",
            "CTS is building a clock distribution network to deliver clock to all flip-flops with minimal skew.",
            "I used ICC2 create_clock and set_clock_uncertainty commands. Target skew was 50ps.",
        ]

        for i, text in enumerate(messages):
            print(f"\n--- Turn {i+1} ---")
            print(f"Candidate: {text}")

            await ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}]
                }
            }))
            await ws.send(json.dumps({"type": "response.create"}))

            audio_chunks = []
            full_text = ""
            first_audio = True
            t0 = time.time()

            async for msg in ws:
                event = json.loads(msg)
                etype = event.get("type", "")

                if etype == "response.output_audio.delta":
                    chunk = base64.b64decode(event["delta"])
                    audio_chunks.append(chunk)
                    if first_audio:
                        first_audio = False
                        print(f"  First audio: {round((time.time()-t0)*1000)}ms")

                elif etype == "response.text.delta":
                    full_text += event.get("delta", "")

                elif etype == "response.done":
                    total_ms = round((time.time()-t0)*1000)
                    print(f"  Ranjitha: {full_text}")
                    print(f"  Total: {total_ms}ms | Audio chunks: {len(audio_chunks)}")
                    break

                elif etype == "error":
                    print(f"  ERROR: {event}")
                    break

            # Save last turn audio
            if audio_chunks and i == len(messages) - 1:
                raw = b"".join(audio_chunks)
                with wave.open(f"grok_voice_{voice}.wav", "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(SAMPLE_RATE)
                    wf.writeframes(raw)
                print(f"  Saved: grok_voice_{voice}.wav")

            await asyncio.sleep(0.5)


if __name__ == "__main__":
    voice = "ara"
    mode = "text"

    for arg in sys.argv[1:]:
        if arg in VOICES:
            voice = arg
        elif arg == "text":
            mode = "text"
        elif arg == "mic" or arg == "realtime":
            mode = "realtime"

    if mode == "realtime":
        asyncio.run(test_voice_agent_realtime(voice))
    else:
        asyncio.run(test_text_mode(voice))
