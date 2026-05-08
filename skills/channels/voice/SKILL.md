# channels/voice

Use when a `voice:utterance` event arrives. Tells you how to reply by voice and how to format text so it sounds right through TTS.

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
3. **You reply** by running `say "your reply text"` in your shell. That's it. No outbox, no file.

If you `say` something, that IS your reply — don't also write the same sentence as your turn output. The user already heard it; repeating it as text puts a duplicate line in the chat.

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
- **STT errors.** The transcript may have errors — "set a timer for ten" might come in as "send a timer for ten". Interpret charitably; ask only when truly ambiguous.

## Quoting

`say` is invoked via shell, so quote with care:

```
say "Got it. The build is green."
```

If your reply contains literal double-quotes, escape them or use single quotes.

## Path conventions

- **Captures:** `sys/drivers/voice/captures/<iso>.wav` — read-only audit trail. You normally don't need to open these; the transcript is in the event payload.

## Troubleshooting

- **You spoke and nothing happened.** Check `/proc/voice-in/log`. Was wake detected? Was a capture WAV written? Did STT produce text?
- **`voice:wake_failed` events appearing.** Mic likely disconnected, or whisper-cli/model is missing from `usr/libexec/voice/`. Re-run `paiman install voice --force` to rebuild.

## Future knobs (not yet in MVP)

- Custom "PAI" wake model (training planned).
- ElevenLabs TTS swap (replace `say` invocation).
- Barge-in (interrupting `say` when wake fires again).
