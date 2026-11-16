#!/usr/bin/env python3
"""lahja-voice: turn a spoken-word script into mp3 files with the ElevenLabs
text-to-speech REST API.

Safe by design: with the dry subcommand nothing is read from the environment and no
network connection is opened. Python 3.8+, standard library only.

Exit codes: 0 ok, 1 tool error, 2 refusal (cap, bad host, existing output,
key passed on the command line).
"""
import argparse
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

VERSION = "0.1.0"
KEY_ENV = "ELEVENLABS_API_KEY"
DEFAULT_MODEL = "eleven_multilingual_v2"
DEFAULT_MAX_CHARS = 5000
CHUNK_LIMIT = 2500            # max characters in one request
TIMEOUT = 60                  # seconds per request
MAX_RESPONSE_BYTES = 50 * 1024 * 1024
MAX_SCRIPT_BYTES = 2 * 1024 * 1024
MAX_ERROR_BODY = 300          # characters of an API error body that may be shown
DEFAULT_TAG = r"^\s*\[[^\]]+\]"
REAL_HOST = "api.elevenlabs.io"
REAL_BASE = "https://" + REAL_HOST
PREVIEW = 80

EXIT_OK, EXIT_ERROR, EXIT_REFUSED = 0, 1, 2


class Refusal(Exception):
    """A deliberate refusal (exit code 2)."""


class ToolError(Exception):
    """An ordinary failure (exit code 1)."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # never let argparse echo raw argv
        # argparse messages can quote argv values (possibly a secret): say nothing of them
        raise ToolError("bad arguments (see --help for the options)")


# ---------------------------------------------------------------- text ----

def clean_text(s):
    """Remove control, format (bidi, zero-width, BOM) and surrogate characters.
    Tabs, line and paragraph separators become spaces."""
    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        if ch == "\t" or cat in ("Zl", "Zp"):
            out.append(" ")
        elif cat in ("Cc", "Cf", "Cs", "Co", "Cn"):
            continue
        else:
            out.append(ch)
    return "".join(out).strip()


def redact(text, key):
    """Remove the key (and the header name's value form) from any text."""
    text = str(text)
    if key:
        text = text.replace(key, "[redacted]")
    return text


# ------------------------------------------------------------ arguments ----

_KEYISH = re.compile(r"key|token|secret|passw|auth|credential", re.I)


def refuse_key_options(argv):
    """Refuse any option that looks like it carries a credential. The value is
    never echoed."""
    for a in argv:
        if a == "--":
            break
        if a.startswith("-"):
            name = a.lstrip("-").split("=", 1)[0]
            if _KEYISH.search(name):
                raise Refusal(
                    "refused: the API key can only come from the environment "
                    "variable %s, never from the command line. Set it in your "
                    "own shell (see README), then run again." % KEY_ENV)


def check_api_base(base):
    """Return the base URL without trailing slash, or raise Refusal."""
    if not isinstance(base, str) or re.search(r"[\s\\\x00-\x1f]", base):
        raise Refusal("refused: --api-base is not an allowed address")
    p = urllib.parse.urlsplit(base)
    try:
        port = p.port
    except ValueError:
        raise Refusal("refused: --api-base has an invalid port")
    clean = (not p.query and not p.fragment and p.path in ("", "/")
             and "@" not in p.netloc and p.username is None)
    if clean and p.scheme == "https" and p.netloc == REAL_HOST:
        return REAL_BASE
    if clean and p.scheme == "http" and p.hostname == "127.0.0.1" and port \
            and p.netloc == "127.0.0.1:%d" % port:
        return "http://127.0.0.1:%d" % port
    raise Refusal("refused: --api-base must be %s or http://127.0.0.1:PORT "
                  "(the key is only ever sent to those)" % REAL_BASE)


def is_loopback(base):
    return base.startswith("http://127.0.0.1:")


def check_ids(voice_id, model):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", voice_id or ""):
        raise Refusal("refused: --voice-id may only contain letters, digits, "
                      "'_' and '-' (max 64)")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", model or ""):
        raise Refusal("refused: --model may only contain letters, digits, "
                      "'_', '.' and '-' (max 64)")


# ----------------------------------------------------------------- plan ----

def split_long(text, limit=CHUNK_LIMIT):
    """Split text into pieces of at most `limit` characters, preferring
    sentence ends, then spaces."""
    pieces = []
    while len(text) > limit:
        window = text[:limit]
        cut = max(window.rfind(c) for c in ".!?؟۔…;؛") + 1
        if cut < limit // 2:
            cut = window.rfind(" ") + 1
        if cut < limit // 4:
            cut = limit
        pieces.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        pieces.append(text)
    return [p for p in pieces if p]


def read_script(path):
    if not os.path.isfile(path):
        raise ToolError("script file not found: " + clean_text(path)[:200])
    if os.path.getsize(path) > MAX_SCRIPT_BYTES:
        raise ToolError("script file is larger than %d bytes" % MAX_SCRIPT_BYTES)
    with open(path, "rb") as f:
        raw = f.read()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ToolError("script file is not valid UTF-8")


def compile_tag(pattern):
    if len(pattern) > 300:
        raise ToolError("--tag regex is longer than 300 characters")
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ToolError("--tag is not a valid regex: " + str(exc)[:100])


def spoken_lines(text, mode, tag_re):
    """Return the cleaned spoken lines. The tag is never spoken.
    mode: 'auto' (tagged lines if the script has any, else all lines),
    'tagged' or 'all'."""
    tagged, plain = [], []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        m = tag_re.match(raw)
        if m and m.end() > 0:
            tagged.append(clean_text(raw[m.end():]))
        else:
            plain.append(clean_text(raw))
    tagged = [t for t in tagged if t]
    plain = [t for t in plain if t]
    if mode == "tagged":
        return tagged
    if mode == "all":
        # keep original order
        out = []
        for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            m = tag_re.match(raw)
            t = clean_text(raw[m.end():] if m and m.end() > 0 else raw)
            if t:
                out.append(t)
        return out
    return tagged if tagged else plain


def build_plan(text, mode, tag_re, out, max_chars):
    """Pure function: decide exactly what would be sent and where it lands."""
    lines = spoken_lines(text, mode, tag_re)
    requests = []
    for ln in lines:
        requests.extend(split_long(ln))
    total = sum(len(r) for r in requests)
    if not requests:
        raise ToolError("no spoken lines found (try --all-lines or --tag)")
    if total > max_chars:
        raise Refusal("refused: %d characters is above --max-chars %d "
                      "(nothing was sent)" % (total, max_chars))
    single = out.lower().endswith(".mp3")
    if single:
        if len(requests) != 1:
            raise Refusal("refused: --out ends in .mp3 but the script needs "
                          "%d requests; give a folder instead" % len(requests))
        files = [out]
    else:
        files = [os.path.join(out, "line_%03d.mp3" % (i + 1)).replace("\\", "/")
                 for i in range(len(requests))]
    return {"lines": lines, "requests": requests, "chars": total,
            "files": files, "single_file": single, "out": out}


def check_out(plan):
    out = plan["out"]
    if plan["single_file"]:
        if os.path.lexists(out):
            raise Refusal("refused: %s already exists (never overwritten)"
                          % clean_text(out)[:200])
        parent = os.path.dirname(os.path.abspath(out))
        if not os.path.isdir(parent):
            raise ToolError("folder for --out does not exist")
        return
    if os.path.lexists(out):
        if not os.path.isdir(out) or os.listdir(out):
            raise Refusal("refused: %s exists and is not an empty folder "
                          "(never overwritten)" % clean_text(out)[:200])


# -------------------------------------------------------------- network ----

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_key():
    """The only place the key is read. Called only on the --send path."""
    key = os.environ.get(KEY_ENV, "")
    if not key:
        raise ToolError(
            "%s is not set. Nothing was sent. Create a key (README: 'Create an "
            "API key') and set it in your own shell, for example in PowerShell: "
            "$env:%s = '<your key>'" % (KEY_ENV, KEY_ENV))
    if not re.fullmatch(r"[\x21-\x7e]{8,256}", key):
        raise ToolError("%s does not look like an API key (printable ASCII, "
                        "no spaces, 8-256 characters). The value is not shown."
                        % KEY_ENV)
    return key


def make_opener(base):
    handlers = [_NoRedirect()]
    if is_loopback(base):
        handlers.insert(0, urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def synthesize(opener, base, key, voice_id, model, text):
    """POST one request; return the mp3 bytes. Never leaks the key."""
    url = "%s/v1/text-to-speech/%s" % (base, voice_id)
    p = urllib.parse.urlsplit(url)
    if p.hostname not in (REAL_HOST, "127.0.0.1"):  # defence in depth
        raise Refusal("refused: host not allowed")
    body = json.dumps({"text": text, "model_id": model}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "xi-api-key": key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
        "User-Agent": "lahja-voice/" + VERSION,
    })
    try:
        resp = opener.open(req, timeout=TIMEOUT)
    except urllib.error.HTTPError as exc:
        code = exc.code
        if 300 <= code < 400:
            raise ToolError("the server answered with a redirect (HTTP %d); "
                            "redirects are never followed" % code)
        try:
            snippet = exc.read(MAX_ERROR_BODY * 4).decode("utf-8", "replace")
        except Exception:
            snippet = ""
        snippet = clean_text(redact(snippet, key))[:MAX_ERROR_BODY]
        raise ToolError("API error HTTP %d%s" % (code, ": " + snippet if snippet else ""))
    except urllib.error.URLError as exc:
        raise ToolError("network error: " + clean_text(redact(exc.reason, key))[:200])
    except (OSError, ValueError) as exc:
        raise ToolError("request failed (%s)" % type(exc).__name__)
    try:
        with resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "audio/mpeg":
                raise ToolError("unexpected content type (expected audio/mpeg)")
            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                raise ToolError("response is larger than the size cap")
            data = bytearray()
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise ToolError("response is larger than the size cap")
    except ToolError:
        raise
    except (OSError, ValueError) as exc:
        raise ToolError("reading the response failed (%s)" % type(exc).__name__)
    if not data:
        raise ToolError("empty audio response")
    return bytes(data)


# ----------------------------------------------------------------- main ----

def print_dry_run(plan, voice_id, model):
    p = print
    p("DRY RUN (nothing is sent; use the send subcommand to send)")
    p("spoken lines: %d" % len(plan["lines"]))
    p("requests: %d" % len(plan["requests"]))
    p("characters: %d" % plan["chars"])
    p("voice id: %s" % voice_id)
    p("model: %s" % model)
    if plan["single_file"]:
        p("destination file: %s" % plan["files"][0])
    else:
        p("destination folder: %s (files %s ... %s)" % (
            plan["out"], os.path.basename(plan["files"][0]),
            os.path.basename(plan["files"][-1])))
    for i, r in enumerate(plan["requests"], 1):
        more = "..." if len(r) > PREVIEW else ""
        p("%03d [%d chars] %s%s" % (i, len(r), r[:PREVIEW], more))
    p("This is billed by characters according to your ElevenLabs plan; "
      "nothing was sent.")


def run(argv):
    refuse_key_options(argv)
    if "--send" in argv:
        raise ToolError("there is no --send flag: use the send subcommand "
                        "(lahja_voice.py send <script> ...)")
    if not argv or argv[0] not in ("dry", "send"):
        raise ToolError("first argument must be a subcommand: dry (no key, no "
                        "network) or send (needs %s, sends the text)" % KEY_ENV)
    sending = argv[0] == "send"
    argv = argv[1:]
    ap = _Parser(prog="lahja_voice.py", allow_abbrev=False,
                 description="Spoken-word script to mp3 with ElevenLabs "
                             "(dry = preview only, send = call the API).")
    ap.add_argument("script")
    ap.add_argument("--voice-id", required=True)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--out", default=None)
    ap.add_argument("--tagged-only", action="store_true")
    ap.add_argument("--all-lines", action="store_true")
    ap.add_argument("--tag", default=DEFAULT_TAG)
    ap.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    ap.add_argument("--api-base", default=REAL_BASE)
    a = ap.parse_args(argv)

    if a.tagged_only and a.all_lines:
        raise ToolError("--tagged-only and --all-lines cannot be combined")
    if a.max_chars < 1:
        raise ToolError("--max-chars must be a positive number")
    mode = "tagged" if a.tagged_only else "all" if a.all_lines else "auto"

    base = check_api_base(a.api_base)
    check_ids(a.voice_id, a.model)
    tag_re = compile_tag(a.tag)
    text = read_script(a.script)
    out = a.out
    if out is None:
        stem = re.sub(r"[^A-Za-z0-9_-]", "_", os.path.splitext(os.path.basename(a.script))[0])[:40]
        out = (stem or "script") + "_voice"
    out = out.replace("\\", "/")
    plan = build_plan(text, mode, tag_re, out, a.max_chars)
    check_out(plan)

    if not sending:
        print_dry_run(plan, a.voice_id, a.model)
        return EXIT_OK

    key = read_key()
    done = 0
    try:
        opener = make_opener(base)
        if not plan["single_file"]:
            os.makedirs(plan["out"], exist_ok=True)
        for req_text, dest in zip(plan["requests"], plan["files"]):
            audio = synthesize(opener, base, key, a.voice_id, a.model, req_text)
            try:
                with open(dest, "xb") as f:
                    f.write(audio)
            except FileExistsError:
                raise Refusal("refused: %s appeared meanwhile (never overwritten)" % dest)
            except OSError as exc:
                raise ToolError("could not write %s (%s)" % (dest, type(exc).__name__))
            done += 1
            print("wrote %s (%d bytes)" % (dest, len(audio)))
        print("done: %d file(s), %d characters sent" % (done, plan["chars"]))
        return EXIT_OK
    except (ToolError, Refusal) as exc:
        done_msg = " (%d file(s) were written before the stop)" % done if done else ""
        raise type(exc)(redact(exc, key) + done_msg)
    except Exception as exc:  # last resort: class name only, never the text
        raise ToolError("unexpected failure (%s)" % type(exc).__name__)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        return run(argv)
    except Refusal as exc:
        print(clean_text(str(exc)), file=sys.stderr)
        return EXIT_REFUSED
    except ToolError as exc:
        print("FAIL: " + clean_text(str(exc)), file=sys.stderr)
        return EXIT_ERROR
    except SystemExit as exc:  # --help
        return exc.code if isinstance(exc.code, int) else EXIT_OK


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(main())
