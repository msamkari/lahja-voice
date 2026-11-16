# Changelog

## 0.1.0 (unreleased)
- First version: one skill and one standard-library Python script (3.8+). Turns a spoken-word script (lines such as `[narrator] text`, the files the `lahja` plugin lints) into mp3 files with the ElevenLabs text-to-speech REST API.
- `dry` prints lines, requests, characters, voice, model, destination and the first 80 characters of each line; reads no key and opens no connection.
- The CLI has two subcommands: `dry` (no key read, no network) and `send` (the only way to use the network). The skill pre-approves only `dry`, so Claude Code asks the user for permission when `send` runs. There is no `--send` flag; passing it says "use the send subcommand". The key comes only from `ELEVENLABS_API_KEY`; key-like command-line options are refused; the key is redacted from every message.
- Only `https://api.elevenlabs.io` (or `http://127.0.0.1:PORT` for tests); redirects are refused; 60 s timeout; 50 MB response cap; `audio/mpeg` only; `--max-chars` (default 5000) is a hard cap; output is new files only.
- TODO: the plugin icon (square PNG, 512-2048 px) for the directory listing; `claude plugin validate` warns about it. A real API call has not been made by the author; the request format follows the public documentation and still needs one live check by the user.
