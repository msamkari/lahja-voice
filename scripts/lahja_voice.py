#!/usr/bin/env python3
"""lahja-voice: turn a spoken-word script into mp3 files with the ElevenLabs
text-to-speech REST API.

Safe by design: the dry subcommand reads no API key and opens no network connection
(it only reads one regular local script file). Python 3.8+, standard library only.

Exit codes: 0 ok, 1 tool error, 2 refusal (cap, bad host, existing output,
key passed on the command line).
"""
import argparse
import http.client
import json
import os
import re
import stat
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import warnings

VERSION = "0.1.1"
KEY_ENV = "ELEVENLABS_API_KEY"
DEFAULT_MODEL = "eleven_multilingual_v2"
DEFAULT_MAX_CHARS = 5000
CHUNK_LIMIT = 2500            # max characters in one request
TIMEOUT = 60                  # seconds per socket read
TOTAL_TIMEOUT = 120           # seconds for one whole request (connect to last byte)
MAX_RESPONSE_BYTES = 50 * 1024 * 1024
MAX_SCRIPT_BYTES = 2 * 1024 * 1024
MAX_ERROR_BODY = 300          # characters of an API error body that may be shown
DEFAULT_TAG = r"^\s*\[[^\]]+\]"
REAL_HOST = "api.elevenlabs.io"
REAL_BASE = "https://" + REAL_HOST
PREVIEW = 80
MAX_PREVIEW_LINES = 50        # requests listed by dry; the rest is only counted
TAG_WINDOW = 256              # the tag regex only sees the first 256 characters of a line
TAG_BUDGET = 5.0              # seconds for all tag matching of one script
MAX_QUANT = 3                 # unbounded quantifiers allowed in --tag

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


_MIN_RUN = 6


def redact(text, key):
    """Remove the key, and any run of 6 or more consecutive characters that also
    occur in the key, from text (so a cut-off key is not shown either)."""
    text = str(text)
    if not key:
        return text
    text = text.replace(key, "[redacted]")
    if len(key) < _MIN_RUN or len(text) < _MIN_RUN:
        return text
    grams = {key[i:i + _MIN_RUN] for i in range(len(key) - _MIN_RUN + 1)}
    hit = [False] * len(text)
    found = False
    for j in range(len(text) - _MIN_RUN + 1):
        if text[j:j + _MIN_RUN] in grams:
            found = True
            for k in range(j, j + _MIN_RUN):
                hit[k] = True
    if not found:
        return text
    out = []
    prev = False
    for ch, h in zip(text, hit):
        if h:
            if not prev:
                out.append("[redacted]")
        else:
            out.append(ch)
        prev = h
    return "".join(out)


def safe(text, key=None, limit=300):
    """Make any string fit to echo: key redacted before and after cleaning (cleaning
    can re-join a key split by invisible characters), control characters removed,
    then truncated."""
    return redact(clean_text(redact(text, key)), key)[:limit]


# ------------------------------------------------------------ arguments ----

_KEYISH = re.compile(r"key|token|secret|passw|auth|credential", re.I)
_KEYSHAPE = re.compile(r"sk[_-][A-Za-z0-9]{20,}")


def refuse_key_options(argv):
    """Refuse any option that looks like it carries a credential. The value is
    never echoed."""
    for a in argv:
        if _KEYSHAPE.search(a):
            raise Refusal(
                "refused: an argument looks like an API key. The key can only "
                "come from the environment variable %s; the argument is not "
                "shown." % KEY_ENV)
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


_RESERVED = re.compile(r"(CON|PRN|AUX|NUL|COM[1-9¹²³]|"
                       r"LPT[1-9¹²³]|CONIN\$|CONOUT\$)", re.I)


def check_path_arg(path, label, strict_name=False):
    """Refuse network (UNC), device, alternate-stream and control-character paths
    BEFORE any filesystem call. The value is never echoed."""
    bad = ("refused: %s is not an allowed path (no UNC or network paths, no ':' "
           "except a drive letter, no control characters, no empty value" % label)
    if strict_name:
        bad += ", no trailing dot or space, no reserved device names"
    bad += ")"
    if not isinstance(path, str) or not path.strip():
        raise Refusal(bad)
    if any(unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Zl", "Zp") for ch in path):
        raise Refusal(bad)
    if len(path) >= 2 and path[0] in "/\\" and path[1] in "/\\":
        raise Refusal(bad)
    rest = path[2:] if re.match(r"[A-Za-z]:[\\/]", path) else path
    if ":" in rest:
        raise Refusal(bad)
    if strict_name:
        for comp in re.split(r"[\\/]+", path):
            if comp in ("", ".", "..") or re.fullmatch(r"[A-Za-z]:", comp):
                continue
            if comp.endswith(".") or comp.endswith(" "):
                raise Refusal(bad)
            if _RESERVED.fullmatch(comp.split(".")[0].rstrip(" ")):
                raise Refusal(bad)


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
    """Read one regular local file, at most MAX_SCRIPT_BYTES. Directories, devices
    and pipes are refused."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        raise ToolError("script file not found or not a regular file: " + safe(path))
    if not stat.S_ISREG(st.st_mode):
        raise ToolError("script file not found or not a regular file: " + safe(path))
    if st.st_size > MAX_SCRIPT_BYTES:
        raise ToolError("script file is larger than %d bytes" % MAX_SCRIPT_BYTES)
    try:
        with open(path, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                raise ToolError("script file is not a regular file")
            raw = f.read(MAX_SCRIPT_BYTES + 1)
    except ToolError:
        raise
    except (OSError, ValueError) as exc:
        raise ToolError("could not read the script file (%s)" % type(exc).__name__)
    if len(raw) > MAX_SCRIPT_BYTES:
        raise ToolError("script file is larger than %d bytes" % MAX_SCRIPT_BYTES)
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ToolError("script file is not valid UTF-8")


_BRACE = re.compile(r"\{(\d*)(?:,(\d*))?\}")


def check_tag_structure(p):
    """Structural guard against catastrophic regexes: no backreferences or
    lookarounds, no quantified group that contains a quantifier or an
    alternation, at most MAX_QUANT unbounded quantifiers, repeat counts <= 100."""
    stack = []
    unbounded = optional = 0
    i, n = 0, len(p)
    bad = ToolError("--tag is not accepted: it must be a simple pattern (no "
                    "backreferences or lookarounds, no repeated group containing a "
                    "repeat or '|', at most %d repeats such as * + {n,}, counts "
                    "up to 100)" % MAX_QUANT)
    while i < n:
        c = p[i]
        if c == "\\":
            if i + 1 < n and p[i + 1] in "123456789":
                raise bad
            i += 2
            continue
        if c == "[":
            j = i + 1
            if j < n and p[j] == "^":
                j += 1
            if j < n and p[j] == "]":
                j += 1
            while j < n and p[j] != "]":
                j += 2 if p[j] == "\\" else 1
            i = j + 1
            continue
        if c == "(":
            if p.startswith("(?", i):
                if p.startswith("(?:", i):
                    i += 3
                else:
                    m = re.match(r"\(\?[aiLmsux]+\)", p[i:])
                    if not m:
                        raise bad
                    i += m.end()
                    continue
            else:
                i += 1
            stack.append(False)
            continue
        if c == ")":
            inner = stack.pop() if stack else False
            i += 1
            quantified = False
            if i < n and p[i] in "*+?":
                quantified = True
            elif i < n and p[i] == "{" and _BRACE.match(p, i):
                quantified = True
            if quantified and inner:
                raise bad
            if stack and (inner or quantified):
                stack[-1] = True
            continue
        if c == "|":
            if stack:
                stack[-1] = True
            i += 1
            continue
        if c in "*+?" or (c == "{" and _BRACE.match(p, i)):
            if c == "{":
                m = _BRACE.match(p, i)
                if not (m.group(1) or m.group(2)):
                    i += 1
                    continue
                if any(g and int(g) > 100 for g in m.groups()):
                    raise bad
                i = m.end()
            else:
                i += 1
            if c == "?":
                optional += 1
            else:
                unbounded += 1
            if i < n and p[i] in "?+":   # lazy / possessive suffix
                i += 1
            if stack:
                stack[-1] = True
            continue
        i += 1
    if unbounded > MAX_QUANT or optional > 6:
        raise bad


def compile_tag(pattern):
    if len(pattern) > 300:
        raise ToolError("--tag regex is longer than 300 characters")
    check_tag_structure(pattern)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        try:
            return re.compile(pattern)
        except (re.error, Warning, OverflowError, RecursionError, ValueError) as exc:
            raise ToolError("--tag is not a valid regex (%s): %s"
                            % (type(exc).__name__, safe(exc, None, 100)))


_LINE_BREAKS = re.compile("\r\n|[\n\r\x0b\x0c\x1c\x1d\x1e\x85  ]")


def spoken_lines(text, mode, tag_re):
    """Return the cleaned spoken lines. A tag that the regex matches is removed
    (the tag only has to match within the first TAG_WINDOW characters).
    mode: 'auto' (tagged lines if the script has any, else all lines),
    'tagged' or 'all'."""
    tagged, plain, both = [], [], []
    t0 = time.monotonic()
    for raw in _LINE_BREAKS.split(text):
        if time.monotonic() - t0 > TAG_BUDGET:
            raise ToolError("--tag matching took too long; use a simpler pattern")
        m = tag_re.match(raw[:TAG_WINDOW])
        is_tag = bool(m and m.end() > 0)
        t = clean_text(raw[m.end():] if is_tag else raw)
        if t:
            (tagged if is_tag else plain).append(t)
            both.append(t)
    if mode == "tagged":
        return tagged
    if mode == "all":
        return both
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
    """Only says whether the destination is usable; never echoes the path."""
    out = plan["out"]
    exists_msg = "refused: --out already exists and is not an empty folder (never overwritten)"
    try:
        if plan["single_file"]:
            if os.path.lexists(out):
                raise Refusal(exists_msg)
            parent = os.path.dirname(os.path.abspath(out))
            if not os.path.isdir(parent):
                raise ToolError("the folder for --out does not exist")
            return
        if os.path.lexists(out):
            if not os.path.isdir(out) or os.listdir(out):
                raise Refusal(exists_msg)
    except (OSError, ValueError) as exc:
        raise ToolError("could not inspect --out (%s)" % type(exc).__name__)


# -------------------------------------------------------------- network ----

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_key():
    """The only place the key is read. Called only on the send path."""
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


def looks_like_mp3(data):
    """ID3 tag or MPEG audio frame sync at the start."""
    if data[:3] == b"ID3":
        return True
    return len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0


def _synthesize(opener, base, key, voice_id, model, text):
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
        try:
            exc.close()
        except Exception:
            pass
        snippet = safe(snippet, key, MAX_ERROR_BODY)
        raise ToolError("API error HTTP %d%s" % (code, ": " + snippet if snippet else ""))
    except urllib.error.URLError as exc:
        raise ToolError("network error: " + safe(exc.reason, key, 200))
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise ToolError("request failed (%s)" % type(exc).__name__)
    try:
        with resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "audio/mpeg":
                raise ToolError("unexpected content type (expected audio/mpeg)")
            declared = resp.headers.get("Content-Length")
            chunked = "chunked" in (resp.headers.get("Transfer-Encoding") or "").lower()
            expected = None
            if declared is not None:
                if not re.fullmatch(r"[0-9]{1,12}", declared.strip()):
                    raise ToolError("invalid Content-Length in the response")
                expected = int(declared)
                if expected > MAX_RESPONSE_BYTES:
                    raise ToolError("response is larger than the size cap")
            elif not chunked:
                raise ToolError("the response has no Content-Length and is not "
                                "chunked, so its completeness cannot be verified")
            data = bytearray()
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise ToolError("response is larger than the size cap")
            if expected is not None and len(data) != expected:
                raise ToolError("incomplete response (%d of %d bytes received)"
                                % (len(data), expected))
    except ToolError:
        raise
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise ToolError("reading the response failed (%s)" % type(exc).__name__)
    if not data:
        raise ToolError("empty audio response")
    if not looks_like_mp3(data):
        raise ToolError("the response is not MP3 audio (no ID3 tag or MPEG "
                        "frame header); nothing was saved")
    return bytes(data)


def synthesize(opener, base, key, voice_id, model, text):
    """POST one request; return the mp3 bytes. Never leaks the key. The whole
    request (connect to last byte) is abandoned after TOTAL_TIMEOUT seconds."""
    box = {}

    def work():
        try:
            box["data"] = _synthesize(opener, base, key, voice_id, model, text)
        except BaseException as exc:  # handed to the caller below
            box["exc"] = exc

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(TOTAL_TIMEOUT)
    if t.is_alive():
        raise ToolError("the request exceeded the total time limit of %d s"
                        % TOTAL_TIMEOUT)
    if "exc" in box:
        raise box["exc"]
    return box["data"]


def keep_recovery(audio):
    """Keep billed audio that could not be saved; return a sentence for the user."""
    try:
        fd, rp = tempfile.mkstemp(prefix="lahja_voice_recovery_", suffix=".mp3")
        with os.fdopen(fd, "wb") as f:
            f.write(audio)
        return "the audio (%d bytes) was kept in %s" % (len(audio), safe(rp))
    except Exception:
        return ("the audio (%d bytes) could NOT be kept; running again bills "
                "again" % len(audio))


def save_audio(dest, audio):
    """Write validated audio to a temporary name next to dest, then move it into
    place without overwriting. On any failure the bytes go to a recovery file."""
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=".lahja_", suffix=".part",
                                   dir=os.path.dirname(dest) or ".")
        with os.fdopen(fd, "wb") as f:
            f.write(audio)
        if os.name == "nt":
            os.rename(tmp, dest)        # fails if dest exists
        else:
            try:
                os.link(tmp, dest)      # fails if dest exists
                os.unlink(tmp)
            except FileExistsError:
                raise
            except OSError:
                if os.path.lexists(dest):
                    raise FileExistsError()
                os.rename(tmp, dest)
        tmp = None
    except FileExistsError:
        note = keep_recovery(audio)
        raise Refusal("refused: %s appeared meanwhile (never overwritten); %s"
                      % (safe(dest), note))
    except (OSError, ValueError) as exc:
        note = keep_recovery(audio)
        raise ToolError("could not write %s (%s); %s"
                        % (safe(dest), type(exc).__name__, note))
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


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
        p("destination file: %s" % safe(plan["files"][0], None, 400))
    else:
        p("destination folder: %s (files %s ... %s)" % (
            safe(plan["out"], None, 400), os.path.basename(plan["files"][0]),
            os.path.basename(plan["files"][-1])))
    reqs = plan["requests"]
    for i, r in enumerate(reqs[:MAX_PREVIEW_LINES], 1):
        more = "..." if len(r) > PREVIEW else ""
        p("%03d [%d chars] %s%s" % (i, len(r), r[:PREVIEW], more))
    if len(reqs) > MAX_PREVIEW_LINES:
        p("... and %d more request(s) not shown" % (len(reqs) - MAX_PREVIEW_LINES))
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

    check_path_arg(a.script, "the script path")
    if a.out is not None:
        check_path_arg(a.out, "--out", strict_name=True)
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
    for label, val in (("--voice-id", a.voice_id), ("--model", a.model),
                       ("--out", out), ("--tag", a.tag), ("script path", a.script)):
        if key in val:
            raise Refusal("refused: the %s value contains the API key; nothing "
                          "was sent" % label)
    done = 0
    try:
        opener = make_opener(base)
        if not plan["single_file"]:
            try:
                os.makedirs(plan["out"], exist_ok=True)
            except (OSError, ValueError) as exc:
                raise ToolError("could not create the output folder (%s)"
                                % type(exc).__name__)
        for req_text, dest in zip(plan["requests"], plan["files"]):
            audio = synthesize(opener, base, key, a.voice_id, a.model, req_text)
            save_audio(dest, audio)
            done += 1
            print("wrote %s (%d bytes)" % (safe(dest, key, 400), len(audio)))
        print("done: %d file(s), %d characters sent" % (done, plan["chars"]))
        return EXIT_OK
    except (ToolError, Refusal) as exc:
        done_msg = " (%d file(s) were written before the stop)" % done if done else ""
        raise type(exc)(safe(exc, key, 1000) + done_msg)
    except Exception as exc:  # last resort: class name only, never the text
        done_msg = " (%d file(s) were written before the stop)" % done if done else ""
        raise ToolError("unexpected failure (%s)%s" % (type(exc).__name__, done_msg))


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
    except Exception as exc:  # never a traceback
        print("FAIL: unexpected failure (%s)" % type(exc).__name__, file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(main())
