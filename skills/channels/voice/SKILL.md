# channels/voice

Use when a `voice:utterance` event arrives and you need to reply by voice — or when deciding whether voice is the right channel for a given response.

## How voice works end-to-end

1. **Inbound.** `voice-in` listens to the mic. On wake word ("hey jarvis" for now), it captures until 1s of silence (or 15s cap), runs whisper.cpp locally, and emits:
   ```yaml
   source: voice
   kind: utterance
   text: "what time is it"
   audio_path: sys/drivers/voice/captures/2026-05-06T14-22-09.wav
   wake_word: hey_jarvis
   duration_ms: 2400
   captured_at: 2026-05-06T14:22:09
   ```
2. **You receive it** if `voice:utterance` is in your `wake_on:`.
3. **You reply** by writing a plain `.txt` file into `outbox/voice/<anything>.txt` inside your instance dir.
4. **Outbound.** `voice-out` watches that dir, runs `say` on the text, and moves the file to `.sent/`.

## When to use voice vs. stay text-only

- The user just spoke to you → reply by voice. Always.
- A background event fires (email arrives, timer trips) and the user is at their desk → text is usually right; voice is intrusive.
- The user explicitly said "tell me out loud" or "speak it" → voice.

## How to format text for TTS

`say` reads literally. Anything that looks fine on a screen will sound terrible aloud.

- **No markdown.** No `**bold**`, no backticks, no bullet characters, no headers. Plain prose.
- **No code blocks.** If you must mention code, describe it ("the function is called get-user-by-id") rather than dumping syntax.
- **Short sentences.** One thought per sentence. Aim for ~15 words max.
- **Expand abbreviations.** "PR" → "pull request". "API" → say "A P I" only if the user uses it that way; otherwise expand.
- **Spell out symbols.** "$5" → "five dollars". "5%" → "five percent". "&" → "and".
- **No URLs.** A URL read aloud is noise. Summarize ("I sent it to your inbox") or omit.
- **Numbers.** Whisper transcribes "two thousand twenty-six" as "2026". When speaking back, prefer the digit form for years and counts; words for small ordinals ("first", not "1st").
- **Length.** Default to under 30 seconds spoken (~75 words). If the answer is longer, give the headline aloud and offer to send the rest as text.

## Path conventions

- **Captures:** `sys/drivers/voice/captures/<iso>.wav` — read-only audit trail. You normally don't need to open these; the transcript is in the event payload.
- **Your outbox:** `~/.pai/var/lib/instances/<your-name>/outbox/voice/<filename>.txt` — anything you write here gets spoken. Filename is arbitrary; a unix timestamp is conventional.
- **Sent audit:** `outbox/voice/.sent/` — files moved here after a successful `say`. Don't write here directly.

## Troubleshooting

- **You spoke and nothing happened.** Check `/proc/voice-in/log` and `/proc/voice-out/log`. Was wake detected? Was a capture WAV written? Did STT produce text?
- **`voice:wake_failed` events appearing.** Mic likely disconnected, or whisper-cli/model is missing from `usr/libexec/voice/`. Re-run `paiman install voice --force` to rebuild.
- **`voice:say_failed` events.** Check the `reason:` field. The original `.txt` is preserved (not moved to `.sent/`) so you can diagnose.

## Future knobs (not yet in MVP)

- Custom "PAI" wake model (training planned).
- ElevenLabs TTS swap (single-knob change in `outbound.py`).
- Barge-in (interrupting `say` when wake fires again).
