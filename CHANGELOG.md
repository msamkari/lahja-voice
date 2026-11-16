# Changelog

## 0.1.2 (2026-11-16)
- Added the plugin icon (square 1024 px PNG, generated with Gemini and chosen by the author).

## 0.1.1
Fixes from the second adversarial round (all with regression tests, local servers only).
- A response is saved only if the received byte count equals `Content-Length` (or the chunked body ends cleanly); a truncated body is an error. A response with neither is refused. The audio must start with an ID3 tag or an MPEG frame sync, so HTML/JSON labelled `audio/mpeg` is refused.
- Files are written to a temporary name and renamed only when complete; if the final write fails the billed audio is kept in a recovery file in the temp folder and its path is printed. `--out` with trailing dot/space, reserved device names, ':' , UNC paths or control characters is refused up front; the same path rules apply to the script path.
- `dry` reads only regular local files (no directories, devices, UNC, ':' paths), at most 2 MB, previews at most 50 requests, and no longer echoes arbitrary `--out` paths.
- Redaction removes the key and any run of 6+ characters of it from every echoed string before truncation; key-shaped arguments are refused; `send` refuses a voice id, model, out, tag or script path containing the key.
- `--tag` has a structural guard (no nested repeats, backreferences, lookarounds; at most 3 unbounded repeats), a 256-character window per line and a 5 s budget; OS errors and unexpected exceptions give one clean line, never a traceback.
- CR, LF, VT, FF, NEL, U+2028 and U+2029 are all line breaks, so a `[tag]` after them is removed and words are not glued.
- Each request is abandoned after 120 s in total, not only per read. The count of files already written is kept in all error messages.
- CI hygiene pattern now catches `sk_` keys (with a test of the pattern).
- Docs: stated exactly which hosts are accepted (the loopback address also receives the key), removed stale and overstated claims, fixed the quoting sentence in SKILL.md.
- Known limits, not fixed: junction/symlink `--out` is followed; a hard cut of a very long line can split a letter from its marks and a sentence search can cut inside a number; `--max-chars` accepts unusual digit forms; `dry` can still preview the first lines of any UTF-8 file it is pointed at; ZWNJ/ZWJ are removed; a real API call has never been made by the author (request format unverified, the refusal of responses without a length is untested against the real service); Python 3.8 was only checked by reading.

## 0.1.0
- First version: one skill and one standard-library Python script (3.8+). Turns a spoken-word script (lines such as `[narrator] text`, the files the `lahja` plugin lints) into mp3 files with the ElevenLabs text-to-speech REST API.
- `dry` prints lines, requests, characters, voice, model, destination and the first 80 characters of each line; reads no key and opens no connection.
- The CLI has two subcommands: `dry` (no key read, no network) and `send` (the only way to use the network). The skill pre-approves only `dry`, so Claude Code asks the user for permission when `send` runs. There is no `--send` flag; passing it says "use the send subcommand". The key comes only from `ELEVENLABS_API_KEY`; key-like command-line options are refused; the key is redacted from every message.
- Only `https://api.elevenlabs.io` (or `http://127.0.0.1:PORT` for tests); redirects are refused; 60 s timeout; 50 MB response cap; `audio/mpeg` only; `--max-chars` (default 5000) is a hard cap; output is new files only.
- TODO: the plugin icon (square PNG, 512-2048 px) for the directory listing; `claude plugin validate` warns about it. A real API call has not been made by the author; the request format follows the public documentation and still needs one live check by the user.
