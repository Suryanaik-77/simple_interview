"""
LiveKit Voice Agent Test — minimal interview agent.

Setup:
    pip install "livekit-agents[codecs]" livekit-plugins-silero livekit-plugins-turn-detector

    # Option A: Use LiveKit Inference (their hosted STT/LLM/TTS)
    # Option B: Use your own API keys (OpenAI, Deepgram)
    pip install livekit-plugins-openai livekit-plugins-deepgram

    # Set env vars:
    export LIVEKIT_URL=wss://your-project.livekit.cloud
    export LIVEKIT_API_KEY=your-api-key
    export LIVEKIT_API_SECRET=your-api-secret
    export OPENAI_API_KEY=your-openai-key
    export DEEPGRAM_API_KEY=your-deepgram-key

    # Run:
    python test_livekit_agent.py dev

    # Then open in browser:
    # https://agents-playground.livekit.io
    # Connect to your LiveKit project and talk.

How to get LiveKit keys (free):
    1. Go to https://cloud.livekit.io
    2. Sign up (free, no credit card)
    3. Create a project
    4. Copy LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET
"""
from dotenv import load_dotenv
load_dotenv()

from livekit import agents
from livekit.agents import AgentSession, Agent, AgentServer
from livekit.plugins import silero

# Try importing providers
try:
    from livekit.agents import inference
    USE_INFERENCE = True
    print("[Setup] Using LiveKit Inference (hosted STT/LLM/TTS)")
except ImportError:
    USE_INFERENCE = False

try:
    from livekit.plugins import openai as lk_openai
    from livekit.plugins import deepgram as lk_deepgram
    USE_PLUGINS = True
    print("[Setup] Using OpenAI + Deepgram plugins")
except ImportError:
    USE_PLUGINS = False


INSTRUCTIONS = """You are Ranjitha, a senior VLSI physical design engineer. 14 years experience. 9 tapeouts.
You are conducting a technical mock interview.

RULES:
- 1-2 sentences per turn. Keep it short and natural.
- React to the candidate's answer, then ask one follow-up question.
- Never teach or explain. Just ask and react.
- Cover topics: floorplanning, CTS, STA, timing closure, routing.
- If they say "I don't know" move to next topic.
- Be conversational, like a real interview."""


class Interviewer(Agent):
    def __init__(self):
        super().__init__(instructions=INSTRUCTIONS)


server = AgentServer()


@server.rtc_session(agent_name="interviewer")
async def interview_session(ctx: agents.JobContext):
    # Build STT/LLM/TTS based on available providers
    if USE_INFERENCE:
        stt = inference.STT(model="deepgram/nova-3", language="en")
        llm = inference.LLM(model="openai/gpt-4o-mini")
        tts = inference.TTS(model="cartesia/sonic-3")
    elif USE_PLUGINS:
        stt = lk_deepgram.STT()
        llm = lk_openai.LLM(model="gpt-4o-mini")
        tts = lk_deepgram.TTS(voice="aura-asteria-en")
    else:
        print("[ERROR] No STT/LLM/TTS provider available.")
        print("Install: pip install livekit-plugins-openai livekit-plugins-deepgram")
        return

    session = AgentSession(
        stt=stt,
        llm=llm,
        tts=tts,
        vad=silero.VAD.load(),
    )

    await session.start(
        room=ctx.room,
        agent=Interviewer(),
    )

    print("[Agent] Interview started. Ranjitha is ready.")

    await session.generate_reply(
        instructions="Greet the candidate warmly and ask them to introduce themselves."
    )


if __name__ == "__main__":
    agents.cli.run_app(server)
