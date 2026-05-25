"""
LiveKit Voice Agent Test — minimal interview agent using LiveKit framework.

Setup:
    pip install livekit-agents livekit-plugins-openai livekit-plugins-deepgram livekit-plugins-silero

    # Set env vars:
    export LIVEKIT_URL=wss://your-project.livekit.cloud
    export LIVEKIT_API_KEY=your-api-key
    export LIVEKIT_API_SECRET=your-api-secret
    export OPENAI_API_KEY=your-openai-key
    export DEEPGRAM_API_KEY=your-deepgram-key

    # Run:
    python test_livekit_agent.py

    # Then open the test page in browser:
    # https://agents-playground.livekit.io
    # Connect to your LiveKit project and talk to the agent.

How to get LiveKit keys (free):
    1. Go to https://cloud.livekit.io
    2. Sign up (free, no credit card)
    3. Create a project
    4. Copy LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET from dashboard
"""
import asyncio
from dotenv import load_dotenv
load_dotenv()

from livekit.agents import AutoSubscribe, JobContext, WorkerOptions, cli, llm
from livekit.agents.voice_assistant import VoiceAssistant
from livekit.plugins import deepgram, openai, silero


async def entrypoint(ctx: JobContext):
    # Wait for a participant (the candidate) to join
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    print("[Agent] Waiting for candidate to join...")
    participant = await ctx.wait_for_participant()
    print(f"[Agent] Candidate joined: {participant.identity}")

    # Create the voice assistant
    assistant = VoiceAssistant(
        vad=silero.VAD.load(),            # Voice Activity Detection (local, free)
        stt=deepgram.STT(),               # Speech-to-Text (Deepgram)
        llm=openai.LLM(model="gpt-4o-mini"),  # Language Model
        tts=deepgram.TTS(voice="aura-asteria-en"),  # Text-to-Speech (Deepgram)
        chat_ctx=llm.ChatContext().append(
            role="system",
            text="""You are Ranjitha, a senior VLSI physical design engineer. 14 years experience. 9 tapeouts.
You are conducting a technical mock interview.

RULES:
- 1-2 sentences per turn. Keep it short.
- React naturally to the candidate's answer, then ask one follow-up.
- Never teach or explain. Just ask and react.
- Start by greeting the candidate and asking them to introduce themselves.
- Cover topics: floorplanning, CTS, STA, timing closure, routing.
- If they say "I don't know" — move to next topic.
- Be conversational, like a real interview."""
        ),
    )

    # Start the assistant — it will listen, think, and speak automatically
    assistant.start(ctx.room, participant)
    print("[Agent] Interview started. Ranjitha is ready.")

    # Say greeting
    await assistant.say("Hi, welcome to the interview. I'm Ranjitha. Tell me a bit about yourself and your experience.", allow_interruptions=True)


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
