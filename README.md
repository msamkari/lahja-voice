# lahja-voice

[العربية](README.ar.md)

Turn a spoken-word script into mp3 audio with the ElevenLabs text-to-speech API. It works on the same script files the sibling `lahja` plugin lints (lines like `[narrator] text`). One skill, one Python script, Python 3.8+ standard library only, MIT.

## Safe by default

The `dry` subcommand is a preview. It prints the number of spoken lines and characters, the voice id, the model, the destination, and the first 80 characters of each line, then says: billed by characters according to your ElevenLabs plan; nothing was sent. A dry run does not read your API key and does not open a network connection.

## Privacy: what leaves your machine, and when

Only when you run the `send` subcommand: the text of the spoken lines (tags removed, control characters removed), the voice id and the model name go to `https://api.elevenlabs.io`, and your key goes in the `xi-api-key` header to that host only. Nothing else is sent: no file names, no paths, no other script text. ElevenLabs handles the text under its own terms and your plan's settings. The plugin has no telemetry.

## Cost

ElevenLabs bills by characters according to your plan. Every dry run shows the exact character count. `--max-chars` (default 5000) is a hard cap on the total, enforced even with `--send`. Check your plan's price and quota on elevenlabs.io; this plugin cannot know them.

## Create an API key

You do this yourself; nobody else should handle your key.

1. Sign up at elevenlabs.io.
2. Open your Profile, then API Keys, then create a key.
3. Give it only the Text to Speech permission and set a monthly character limit.
4. Set it as an environment variable named `ELEVENLABS_API_KEY`. In PowerShell for the current session: `$env:ELEVENLABS_API_KEY = '...'` (paste your key between the quotes). Or add it to your user environment variables (Windows Settings, search "environment variables") and restart the terminal.
5. Never commit the key and never paste it into a chat. The script refuses `--api-key` style options and never reads a key from a file.

## Usage

```
python scripts/lahja_voice.py dry|send script.md --voice-id VOICE_ID [--model eleven_multilingual_v2] [--out DIR_or_FILE.mp3] [--tagged-only | --all-lines] [--tag REGEX] [--max-chars 5000] [--api-base URL]
```

- Find a voice id in your ElevenLabs voice library.
- Default scope: lines that start with a `[tag]` if the script has any, otherwise every non-empty line. `--tagged-only` and `--all-lines` force one or the other; `--tag` changes the tag regex (default `^\s*\[[^\]]+\]`). The tag is never spoken.
- One request per spoken line; a line over 2500 characters is split at sentence ends. Files are `line_001.mp3`, `line_002.mp3`, ... in a new folder (default `<script name>_voice`). `--out file.mp3` works only for a one-request script.
- Output is new files only: an existing non-empty folder or an existing file is refused (exit 2). Stops at the first API error.
- Text from the script is stripped of control, bidi and zero-width characters and is never treated as options.
- Exit codes: 0 ok, 1 tool error, 2 refusal (cap, bad host, existing output, key on the command line).
- `--api-base` accepts only `https://api.elevenlabs.io` or `http://127.0.0.1:PORT` (for tests). Redirects are never followed. Responses must be `audio/mpeg` and at most 50 MB.

In Claude Code the skill pre-approves only `dry`. It shows you the dry run and runs `send` only after you say yes in chat; `send` is not pre-approved, so Claude Code also asks your permission at that moment. The old `--send` flag no longer exists and gives an error.

## Limits

- Not verified by the author against the live service: the request follows the public API documentation (`POST /v1/text-to-speech/{voice_id}`, JSON `text` and `model_id`).
- Characters are counted as Python characters; ElevenLabs may count slightly differently.
- Arabic dialect pronunciation depends on the model and voice; listen before publishing.
- Proxies from the environment are honored for the real host (not for the loopback test address).
