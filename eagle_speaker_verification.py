"""
Picovoice Eagle Speaker Recognition - Voice Verification Module
On-device, privacy-focused speaker verification for interview authentication
"""
import os
import logging
import base64
import tempfile
import json
from typing import Optional, Tuple, Dict
from secrets_proxy import get_secret

log = logging.getLogger('interview')

# Eagle Configuration
EAGLE_ACCESS_KEY = get_secret("PICOVOICE_ACCESS_KEY", "")
EAGLE_MODEL_PATH = os.path.join(os.path.dirname(__file__), "eagle_models")

# Verification threshold (0.0 to 1.0, higher = stricter)
# Eagle recommends 0.5-0.7 for most use cases
VERIFICATION_THRESHOLD = 0.6

# Minimum audio duration for enrollment (seconds)
MIN_ENROLLMENT_AUDIO_SEC = 3.0

_eagle_profiler = None
_eagle_recognizer = None
_profiles_cache = {}  # session_id -> eagle_profile


def init_eagle():
    """Initialize Eagle speaker recognition engine."""
    global _eagle_profiler, _eagle_recognizer

    if not EAGLE_ACCESS_KEY:
        log.warning("[Eagle] PICOVOICE_ACCESS_KEY not set — voice verification disabled")
        return False

    try:
        import pveagle

        # Create Eagle Profiler (for enrollment)
        _eagle_profiler = pveagle.create_profiler(access_key=EAGLE_ACCESS_KEY)

        # Create Eagle Recognizer (for verification)
        _eagle_recognizer = pveagle.create_recognizer(access_key=EAGLE_ACCESS_KEY)

        log.info(f"[Eagle] Speaker recognition initialized (v{pveagle.__version__})")
        log.info(f"[Eagle] Profiler sample rate: {_eagle_profiler.sample_rate} Hz")
        log.info(f"[Eagle] Min enrollment samples: {_eagle_profiler.min_enroll_samples}")
        return True

    except ImportError:
        log.error("[Eagle] pveagle not installed")
        log.info("[Eagle] Install: pip install pveagle")
        return False
    except Exception as e:
        log.error(f"[Eagle] Initialization failed: {e}")
        return False


def is_available():
    """Check if Eagle speaker recognition is available."""
    return _eagle_profiler is not None and _eagle_recognizer is not None


def _audio_bytes_to_pcm(audio_bytes: bytes) -> Optional[list]:
    """
    Convert audio bytes (any format) to 16kHz mono PCM int16 samples.
    Eagle requires 16-bit PCM audio at its sample_rate.
    """
    try:
        import io
        import soundfile as sf
        import numpy as np

        # Read audio file
        audio, sr = sf.read(io.BytesIO(audio_bytes), dtype='int16')

        # Convert to mono if stereo
        if len(audio.shape) > 1:
            audio = audio.mean(axis=1).astype('int16')

        # Resample to Eagle's required sample rate if needed
        if sr != _eagle_profiler.sample_rate:
            import resampy
            audio = resampy.resample(
                audio.astype('float32'),
                sr,
                _eagle_profiler.sample_rate
            ).astype('int16')

        # Convert numpy array to list for Eagle API
        return audio.tolist()

    except Exception as e:
        log.error(f"[Eagle] Audio conversion failed: {e}")
        return None


def enroll_speaker(audio_bytes: bytes, session_id: str) -> Tuple[bool, str, dict]:
    """
    Enroll a speaker profile from audio.

    Args:
        audio_bytes: Audio data (WAV, MP3, etc.)
        session_id: Session identifier for caching

    Returns:
        (success: bool, profile_data: str or error_msg, metadata: dict)
        profile_data is base64-encoded Eagle profile bytes
    """
    if not _eagle_profiler:
        return False, "Eagle profiler not initialized", {}

    try:
        # Convert audio to PCM
        pcm_samples = _audio_bytes_to_pcm(audio_bytes)
        if not pcm_samples:
            return False, "Failed to convert audio to PCM", {}

        # Check minimum duration
        duration_sec = len(pcm_samples) / _eagle_profiler.sample_rate
        if duration_sec < MIN_ENROLLMENT_AUDIO_SEC:
            return False, f"Audio too short: {duration_sec:.1f}s (need {MIN_ENROLLMENT_AUDIO_SEC}s)", {
                "duration_sec": duration_sec,
                "min_required": MIN_ENROLLMENT_AUDIO_SEC
            }

        # Enroll with Eagle Profiler
        enroll_percentage, feedback = _eagle_profiler.enroll(pcm_samples)

        # Check if enrollment is complete
        if enroll_percentage < 100:
            return False, f"Need more audio: {enroll_percentage}% enrolled", {
                "enrollment_percentage": enroll_percentage,
                "feedback": str(feedback),
                "duration_sec": duration_sec
            }

        # Export the profile
        profile_bytes = _eagle_profiler.export()

        # Encode to base64 for storage
        profile_b64 = base64.b64encode(profile_bytes).decode('ascii')

        # Cache the profile object
        _profiles_cache[session_id] = profile_bytes

        log.info(f"[Eagle] Enrolled speaker for session {session_id[:8]} ({len(profile_bytes)} bytes)")

        return True, profile_b64, {
            "enrollment_percentage": 100,
            "feedback": str(feedback),
            "duration_sec": duration_sec,
            "profile_size_bytes": len(profile_bytes)
        }

    except Exception as e:
        log.error(f"[Eagle] Enrollment failed: {e}")
        return False, f"Enrollment error: {str(e)}", {}


def verify_speaker(
    profile_data: str,
    audio_bytes: bytes,
    session_id: str
) -> Tuple[bool, float, dict]:
    """
    Verify if audio matches the enrolled speaker profile.

    Args:
        profile_data: Base64-encoded Eagle profile
        audio_bytes: Audio data to verify
        session_id: Session identifier

    Returns:
        (match: bool, score: float, details: dict)
        - match: True if speaker verified (score >= threshold)
        - score: Similarity score (0.0 to 1.0)
        - details: Verification metadata
    """
    if not _eagle_recognizer:
        return False, 0.0, {"error": "Eagle recognizer not initialized"}

    try:
        # Get profile bytes (from cache or decode)
        if session_id in _profiles_cache:
            profile_bytes = _profiles_cache[session_id]
        else:
            profile_bytes = base64.b64decode(profile_data)
            _profiles_cache[session_id] = profile_bytes

        # Convert audio to PCM
        pcm_samples = _audio_bytes_to_pcm(audio_bytes)
        if not pcm_samples:
            return False, 0.0, {"error": "Failed to convert audio to PCM"}

        # Verify with Eagle Recognizer
        scores = _eagle_recognizer.process([profile_bytes], pcm_samples)

        # Get similarity score (0.0 to 1.0)
        score = scores[0] if scores else 0.0

        # Determine match
        match = score >= VERIFICATION_THRESHOLD

        details = {
            "score": round(score, 4),
            "threshold": VERIFICATION_THRESHOLD,
            "match": match,
            "audio_duration_sec": round(len(pcm_samples) / _eagle_profiler.sample_rate, 2)
        }

        return match, score, details

    except Exception as e:
        log.error(f"[Eagle] Verification failed: {e}")
        return False, 0.0, {"error": str(e)}


def reset_profiler():
    """Reset the Eagle profiler for a new enrollment."""
    if _eagle_profiler:
        _eagle_profiler.reset()
        log.info("[Eagle] Profiler reset for new enrollment")


def cleanup_session(session_id: str):
    """Remove cached profile for a session."""
    if session_id in _profiles_cache:
        del _profiles_cache[session_id]
        log.info(f"[Eagle] Cleaned up profile cache for session {session_id[:8]}")


# ── Interview Integration Helpers ──────────────────────────────────────

def enroll_reference_voice(audio_bytes: bytes, session_id: str) -> Tuple[bool, str, dict]:
    """
    Simplified enrollment for interview lobby voice capture.

    Returns: (success, profile_data_or_error, metadata)
    """
    reset_profiler()  # Start fresh
    return enroll_speaker(audio_bytes, session_id)


def verify_turn_audio(
    session_id: str,
    profile_data: str,
    audio_bytes: bytes,
    turn: int
) -> dict:
    """
    Verify speaker for a specific interview turn.
    Returns complete result for logging/tracking.
    """
    match, score, details = verify_speaker(profile_data, audio_bytes, session_id)

    result = {
        "turn": turn,
        "verified": match,
        "score": round(score, 4),
        "threshold": VERIFICATION_THRESHOLD,
        "match": match,
        "engine": "eagle",
        **details
    }

    if match:
        log.info(f"[Eagle] Session {session_id[:8]} Turn {turn}: VERIFIED (score={score:.4f})")
    else:
        log.warning(f"[Eagle] Session {session_id[:8]} Turn {turn}: MISMATCH (score={score:.4f})")

    return result


def get_system_info() -> dict:
    """Get Eagle system information for admin dashboard."""
    if not is_available():
        return {
            "available": False,
            "error": "Eagle not initialized"
        }

    try:
        import pveagle

        return {
            "available": True,
            "version": pveagle.__version__,
            "sample_rate": _eagle_profiler.sample_rate,
            "min_enroll_samples": _eagle_profiler.min_enroll_samples,
            "min_enroll_seconds": MIN_ENROLLMENT_AUDIO_SEC,
            "verification_threshold": VERIFICATION_THRESHOLD,
            "cached_profiles": len(_profiles_cache),
            "engine": "Picovoice Eagle"
        }
    except Exception as e:
        return {
            "available": True,
            "error": str(e)
        }


def set_verification_threshold(threshold: float) -> bool:
    """
    Update verification threshold (0.0 to 1.0).
    Higher = stricter verification.
    """
    global VERIFICATION_THRESHOLD

    if not 0.0 <= threshold <= 1.0:
        log.error(f"[Eagle] Invalid threshold: {threshold} (must be 0.0-1.0)")
        return False

    VERIFICATION_THRESHOLD = threshold
    log.info(f"[Eagle] Verification threshold updated to {threshold}")
    return True
