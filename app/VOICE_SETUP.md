# Khayyam AI voice setup (General Chat + Tutor)

This update includes both voice directions:

- **Speech input:** browser microphone recording or audio-file upload -> Flask `/api/transcribe` -> OpenAI transcription -> transcript appears in the message box for review -> user sends it to Chat or Tutor.
- **Speech output:** click **شنیدن پاسخ** on an AI reply -> Flask `/api/tts` -> OpenAI text-to-speech -> browser audio playback.

## Render configuration

Keep these environment variables in Render (never put keys in frontend code):

- `OPENAI_API_KEY` — required for both transcription and speech generation.
- `STT_MODEL` — optional; defaults to `gpt-4o-mini-transcribe`. You can change it to another transcription model available to your API project.
- `TTS_MODEL` — optional; defaults to `gpt-4o-mini-tts`.
- `TTS_VOICE` — optional; defaults to `coral`. Try `marin`, `cedar`, or `coral` and compare actual Dari pronunciation.

Audio API usage is billed separately from ordinary chat usage. Confirm billing and model access in your OpenAI API project.

## Browser checklist

1. Open the deployed site using **HTTPS**. Browsers generally block microphone access on non-secure origins, except localhost.
2. When prompted, allow microphone access for the site. If previously blocked, use the browser's site settings (lock/tune icon beside the address bar) to allow it.
3. Tap 🎙 once to start recording and again to stop. Wait for transcription, review the text, and tap send.
4. If recording is unavailable, use 📁 to upload a recording from your device.
5. If transcription fails, check Render logs and verify `OPENAI_API_KEY`, API billing, and model access.

## Privacy and behavior

The audio is sent to OpenAI for transcription. It is not sent until the user stops recording or chooses an audio file. The transcript is not automatically submitted as a chat message; the user can review/edit it first.

## Limits

The Flask request limit is 20 MB. Keep recordings reasonably short. The built-in TTS voices are optimized for English, so Afghan Dari pronunciation must be evaluated with real audio samples.
