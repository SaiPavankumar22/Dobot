"""The voice call: mic → VAD → local Whisper → streaming LLM → Sarvam TTS, end to end in-process.

The cascade follows huggingface/speech-to-speech's shape (each stage streams into the next, and
new speech cancels whatever is in flight), while every stage is one Dobot already owns or has
already chosen: Silero for endpointing, the local Whisper checkpoint for transcription, the
NVIDIA Nemotron reasoner for answers, and Sarvam's bulbul voices for speech.
"""
