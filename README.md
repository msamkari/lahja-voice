# lahja-voice

[العربية](README.ar.md)

Turn a spoken-word script into mp3 audio with the ElevenLabs text-to-speech API. It works on the same script files the sibling `lahja` plugin lints (lines like `[narrator] text`). One skill, one Python script, Python 3.8+ standard library only, MIT.

## Safe by default

The `dry` subcommand is a preview. It prints the number of spoken lines and characters, the voice id, the model, the destination, and the first 80 characters of each line, then says: billed by characters according to your ElevenLabs plan; nothing was sent. A dry run does not read your API key and the script opens no network socket. It only reads the one script file you name, which must be a regular local file (directories, devices, UNC/network paths and paths with ':' are refused). It lists at most the first 50 requests (80 characters each) and counts the rest.

## Privacy: what leaves your machine, and when

Only when you run the `send` subcommand: the text of the spoken lines (tags removed, control characters removed), the voice id and the model name go to `https://api.elevenlabs.io`, and your key goes in the `xi-api-key` header. The only hosts the script accepts are `https://api.elevenlabs.io` and, for tests, `http://127.0.0.1:PORT`; the loopback address also receives the key, so whatever program listens on that local port sees it. Nothing else is sent: no file names, no paths, no other script text. ElevenLabs handles the text under its own terms and your plan's settings. The plugin has no telemetry.

## Cost

ElevenLabs bills by characters according to your plan. Every dry run shows the exact character count. `--max-chars` (default 5000) is a cap on the total characters; the script refuses (exit 2) before sending anything if the script is above it. Check your plan's price and quota on elevenlabs.io; this plugin cannot know them.

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
- Default scope: lines that start with a `[tag]` if the script has any, otherwise every non-empty line. `--tagged-only` and `--all-lines` force one or the other; `--tag` changes the tag regex (default `^\s*\[[^\]]+\]`). The matched tag is removed from the spoken text. The regex must be simple (no backreferences or lookarounds, no repeated group that contains a repeat or `|`, at most 3 repeats such as `*`, `+`, `{n,}`, counts up to 100) and only sees the first 256 characters of a line. Lines are split at CR, LF, VT, FF, NEL, U+2028 and U+2029.
- One request per spoken line; a line over 2500 characters is split at a sentence end where possible, otherwise at a space, otherwise by a hard cut (which can separate a letter from its marks). Files are `line_001.mp3`, `line_002.mp3`, ... in a new folder (default `<script name>_voice`). `--out file.mp3` works only for a one-request script.
- Output is new files only: an existing non-empty folder or an existing file is refused (exit 2), `--out` must not contain UNC paths, ':' (except a drive letter), control characters, trailing dots or spaces, or reserved device names. Each audio file is written under a temporary name and renamed only after it is complete; if the final write fails, the billed audio is kept in a recovery file in your temp folder and its path is printed. Stops at the first error.
- Text from the script is stripped of control, bidi, zero-width and unassigned characters (this also removes ZWNJ/ZWJ) and is never treated as options. Arguments that look like an API key are refused, and so is a `--voice-id`, `--model`, `--out`, `--tag` or script path that contains your key.
- Exit codes: 0 ok, 1 tool error, 2 refusal (cap, bad host, existing output, key on the command line).
- `--api-base` accepts only `https://api.elevenlabs.io` or `http://127.0.0.1:PORT` (for tests). Redirects are never followed. A response is saved only if its `Content-Type` is `audio/mpeg`, it is at most 50 MB, its received length equals `Content-Length` (or it is chunked and ends cleanly), and it starts with an ID3 tag or an MPEG frame header. One request may take at most 120 s in total.

In Claude Code the skill pre-approves only `dry`. It shows you the dry run and runs `send` only after you say yes in chat; `send` is not pre-approved, so Claude Code also asks your permission at that moment. The old `--send` flag no longer exists and gives an error.

## Limits

- Tried against the real service on 2026-11-27 with model `eleven_v4` only: one short line, then three short lines (all saved as mp3), plus an invalid key (HTTP 401) and an unknown voice id (HTTP 404). Not tried against the real service: the default model, splitting of lines over 2500 characters, quota and rate-limit errors (these ran against a local test server). A response without `Content-Length` that is not chunked is refused.
- `dry` can show the first lines of any readable UTF-8 text file you point it at (at most 50 lines of 80 characters). Only point it at script files.
- Not fixed: an existing junction/symlink as `--out` is followed; characters are counted by Python, and Python 3.8 knows an older Unicode version than newer Pythons.
- Characters are counted as Python characters; ElevenLabs may count slightly differently.
- Arabic dialect pronunciation depends on the model and voice; listen before publishing.
- Proxies from the environment are honored for the real host (not for the loopback test address).
