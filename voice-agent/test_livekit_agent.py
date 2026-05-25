from dotenv import load_dotenv
load_dotenv()

from livekit import agents
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    WorkerOptions,
    cli,
)
from livekit.plugins import silero

# -----------------------------
# Provider setup
# -----------------------------

USE_INFERENCE = False
USE_PLUGINS = False

try:
    from livekit.agents import inference

    USE_INFERENCE = True
    print("[Setup] Using LiveKit Inference (hosted STT/LLM/TTS)")
except ImportError:
    pass

try:
    from livekit.plugins import openai as lk_openai
    from livekit.plugins import deepgram as lk_deepgram

    USE_PLUGINS = True
    print("[Setup] Using OpenAI + Deepgram plugins")
except ImportError:
    pass


# -----------------------------
# Interview Instructions
# -----------------------------

INSTRUCTIONS = """
You are Ranjitha, a senior VLSI physical design engineer with 14 years experience and 9 tapeouts.

You are conducting a realistic mock interview.

RULES:
- Keep responses short and natural.
- Ask only one question at a time.
- React briefly to candidate answers.
- Never teach or explain concepts.
- Cover:
  - Floorplanning
  - Placement
  - CTS
  - STA
  - Routing
  - Timing closure
- If candidate says "I don't know", move to another topic.
- Sound like a real interviewer.
"""


# -----------------------------
# Agent Definition
# -----------------------------

class Interviewer(Agent):
    def __init__(self):
        super().__init__(instructions=INSTRUCTIONS)


# -----------------------------
# Main RTC Session
# -----------------------------

async def entrypoint(ctx: JobContext):

    print("[Agent] Job received")

    await ctx.connect()

    print("[Agent] Connected to room")

    # -----------------------------
    # Choose providers
    # -----------------------------

    if USE_INFERENCE:
        stt = inference.STT(
            model="deepgram/nova-3",
            language="en",
        )

        llm = inference.LLM(
            model="openai/gpt-4o-mini"
        )

        tts = inference.TTS(
            model="cartesia/sonic-3"
        )

    elif USE_PLUGINS:
        stt = lk_deepgram.STT()

        llm = lk_openai.LLM(
            model="gpt-4o-mini"
        )

        tts = lk_deepgram.TTS(
            model="aura-asteria-en"
        )

    else:
        print("[ERROR] No providers installed")
        return

    # -----------------------------
    # Voice Session
    # -----------------------------

    session = AgentSession(
        vad=silero.VAD.load(),
        stt=stt,
        llm=llm,
        tts=tts,
    )

    await session.start(
        room=ctx.room,
        agent=Interviewer(),
    )

    print("[Agent] Interview session started")

    # -----------------------------
    # Initial greeting
    # -----------------------------

    await session.generate_reply(
        instructions="""
        Greet the candidate warmly.
        Ask them to introduce themselves briefly.
        """
    )


# -----------------------------
# Worker Startup
# -----------------------------

if __name__ == "__main__":

    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="interviewer",
        )
    )