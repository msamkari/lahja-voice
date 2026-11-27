import contextlib
import http.server
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import re
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))).replace("\\", "/")
SCRIPT = ROOT + "/scripts/lahja_voice.py"
sys.path.insert(0, ROOT + "/scripts")
import lahja_voice as lv  # noqa: E402

# Built at runtime so no key-like or address-like literal sits in the source.
KEY = "CANARY" + "0123456789" + "abcdef" + "ZYXW"
MP3 = b"ID3" + b"\x00" * 64
EVIL_HOST = "evil" + ".example"


class _H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        srv = self.server
        srv.requests.append({"path": self.path, "headers": dict(self.headers),
                             "body": body})
        beh = srv.behavior
        if beh.get("redirect"):
            self.send_response(302)
            self.send_header("Location", beh["redirect"])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        status = beh.get("status", 200)
        data = beh.get("body", MP3)
        if beh.get("echo_key"):
            data = ("bad key " + self.headers.get("xi-api-key", "")).encode()
        self.send_response(status)
        self.send_header("Content-Type", beh.get("ctype", "audio/mpeg"))
        cl = beh["cl"] if "cl" in beh else str(len(data))
        if cl is not None:
            self.send_header("Content-Length", cl)
        if beh.get("chunked"):
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            if beh.get("drip"):
                for _ in range(beh["drip"]):
                    self.wfile.write(b"I")
                    self.wfile.flush()
                    time.sleep(0.3)
            else:
                self.wfile.write(beh["raw"] if "raw" in beh else data)
        except OSError:
            pass


def start_server(behavior=None):
    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    srv.requests = []
    srv.behavior = behavior or {}
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def stop(srv):
    srv.shutdown()
    srv.server_close()


def base_of(srv):
    return "http://127.0.0.1:%d" % srv.server_address[1]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lv_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def script(self, text, name="s.md", raw=None):
        p = self.tmp + "/" + name
        with open(p, "wb") as f:
            f.write(raw if raw is not None else text.encode("utf-8"))
        return p

    def run_main(self, argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        e = {k: v for k, v in os.environ.items() if k != lv.KEY_ENV}
        e.update(env or {})
        with mock.patch.dict(os.environ, e, clear=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = lv.main(argv)
        return code, out.getvalue(), err.getvalue()

    def fresh_out(self, name="o"):
        return self.tmp + "/" + name


TAGGED = "[narrator] Hello there.\n[calm] Second line.\n# Heading\nplain note\n"


class DryRun(Base):
    def test_default_is_dry_run(self):
        p = self.script(TAGGED)
        code, out, err = self.run_main(["dry", p, "--voice-id", "voiceABC", "--out", self.fresh_out()])
        self.assertEqual(code, 0)
        self.assertIn("nothing was sent", out)
        self.assertIn("billed by characters according to your ElevenLabs plan", out)

    def test_no_network_in_dry_run(self):
        p = self.script(TAGGED)

        def boom(*a, **k):
            raise AssertionError("network touched")
        with mock.patch.object(socket, "socket", boom), \
                mock.patch.object(socket, "create_connection", boom), \
                mock.patch.object(socket, "getaddrinfo", boom):
            code, out, err = self.run_main(["dry", p, "--voice-id", "v1", "--out", self.fresh_out()])
        self.assertEqual(code, 0, err)

    def test_dry_run_never_reads_key(self):
        p = self.script(TAGGED)
        with mock.patch.object(lv, "read_key", side_effect=AssertionError("key read")):
            code, out, err = self.run_main(["dry", p, "--voice-id", "v1", "--out", self.fresh_out()],
                                           env={lv.KEY_ENV: KEY})
        self.assertEqual(code, 0, err)

    def test_dry_run_does_not_create_out(self):
        p = self.script(TAGGED)
        o = self.fresh_out()
        self.run_main(["dry", p, "--voice-id", "v1", "--out", o])
        self.assertFalse(os.path.exists(o))

    def test_dry_run_with_key_set_does_not_print_it(self):
        p = self.script(TAGGED)
        code, out, err = self.run_main(["dry", p, "--voice-id", "v1", "--out", self.fresh_out()],
                                       env={lv.KEY_ENV: KEY})
        self.assertNotIn(KEY, out + err)

    def test_dry_run_reports_counts_voice_model_destination(self):
        p = self.script(TAGGED)
        o = self.fresh_out().replace("\\", "/")
        code, out, err = self.run_main(["dry", p, "--voice-id", "voiceABC", "--out", o])
        self.assertIn("spoken lines: 2", out)
        self.assertIn("characters: %d" % (len("Hello there.") + len("Second line.")), out)
        self.assertIn("voice id: voiceABC", out)
        self.assertIn("model: " + lv.DEFAULT_MODEL, out)
        self.assertIn(o + " (files line_001.mp3 ... line_002.mp3)", out)

    def test_preview_is_80_chars(self):
        p = self.script("[a] " + "x" * 200 + "\n")
        code, out, err = self.run_main(["dry", p, "--voice-id", "v1", "--out", self.fresh_out()])
        line = [l for l in out.splitlines() if l.startswith("001 ")][0]
        self.assertIn("x" * 80 + "...", line)
        self.assertNotIn("x" * 81, line)

    def test_custom_model_shown(self):
        p = self.script(TAGGED)
        code, out, err = self.run_main(["dry", p, "--voice-id", "v1", "--model", "eleven_v3",
                                        "--out", self.fresh_out()])
        self.assertIn("model: eleven_v3", out)

    def test_single_mp3_destination(self):
        p = self.script("[a] one line\n")
        o = (self.fresh_out() + ".mp3").replace("\\", "/")
        code, out, err = self.run_main(["dry", p, "--voice-id", "v1", "--out", o])
        self.assertEqual(code, 0, err)
        self.assertIn("destination file: " + o, out)


class Send(Base):
    def test_send_requires_flag(self):
        srv = start_server()
        self.addCleanup(stop, srv)
        p = self.script(TAGGED)
        code, out, err = self.run_main(["dry", p, "--voice-id", "v1", "--api-base", base_of(srv),
                                        "--out", self.fresh_out()], env={lv.KEY_ENV: KEY})
        self.assertEqual(srv.requests, [])

    def test_send_missing_key_exit_1(self):
        p = self.script(TAGGED)
        o = self.fresh_out()
        code, out, err = self.run_main(["send", p, "--voice-id", "v1", "--out", o])
        self.assertEqual(code, 1)
        self.assertIn(lv.KEY_ENV, err)
        self.assertIn("README", err)
        self.assertFalse(os.path.exists(o))

    def test_send_ok_writes_files(self):
        srv = start_server()
        self.addCleanup(stop, srv)
        p = self.script(TAGGED)
        o = self.fresh_out()
        code, out, err = self.run_main(["send", p, "--voice-id", "voiceABC", "--api-base", base_of(srv), "--out", o],
                                       env={lv.KEY_ENV: KEY})
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(os.listdir(o)), ["line_001.mp3", "line_002.mp3"])
        with open(o + "/line_001.mp3", "rb") as f:
            self.assertEqual(f.read(), MP3)
        self.assertEqual(len(srv.requests), 2)

    def test_request_shape(self):
        srv = start_server()
        self.addCleanup(stop, srv)
        p = self.script("[a] Hello\n")
        self.run_main(["send", p, "--voice-id", "voiceABC", "--model", "m1", "--api-base", base_of(srv), "--out", self.fresh_out()],
                      env={lv.KEY_ENV: KEY})
        r = srv.requests[0]
        self.assertEqual(r["path"], "/v1/text-to-speech/voiceABC")
        self.assertEqual({k.lower(): v for k, v in r["headers"].items()}["xi-api-key"], KEY)
        self.assertEqual(json.loads(r["body"].decode("utf-8")),
                         {"text": "Hello", "model_id": "m1"})

    def test_single_file_out(self):
        srv = start_server()
        self.addCleanup(stop, srv)
        p = self.script("[a] Hello\n")
        o = self.fresh_out() + ".mp3"
        code, out, err = self.run_main(["send", p, "--voice-id", "v1", "--api-base", base_of(srv), "--out", o],
                                       env={lv.KEY_ENV: KEY})
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.isfile(o))

    def test_key_never_in_output_on_success(self):
        srv = start_server()
        self.addCleanup(stop, srv)
        p = self.script(TAGGED)
        code, out, err = self.run_main(["send", p, "--voice-id", "v1", "--api-base", base_of(srv), "--out", self.fresh_out()],
                                       env={lv.KEY_ENV: KEY})
        self.assertNotIn(KEY, out + err)

    def test_api_error_stops_and_hides_key(self):
        srv = start_server({"status": 401, "ctype": "application/json",
                            "body": b'{"detail":"unauthorized"}'})
        self.addCleanup(stop, srv)
        p = self.script(TAGGED)
        code, out, err = self.run_main(["send", p, "--voice-id", "v1", "--api-base", base_of(srv), "--out", self.fresh_out()],
                                       env={lv.KEY_ENV: KEY})
        self.assertEqual(code, 1)
        self.assertIn("HTTP 401", err)
        self.assertEqual(len(srv.requests), 1)  # stopped at the first error
        self.assertNotIn(KEY, out + err)

    def test_server_echoing_key_is_redacted(self):
        srv = start_server({"status": 401, "ctype": "text/plain", "echo_key": True})
        self.addCleanup(stop, srv)
        p = self.script(TAGGED)
        code, out, err = self.run_main(["send", p, "--voice-id", "v1", "--api-base", base_of(srv), "--out", self.fresh_out()],
                                       env={lv.KEY_ENV: KEY})
        self.assertEqual(code, 1)
        self.assertNotIn(KEY, out + err)
        self.assertIn("[redacted]", err)

    def test_error_body_control_chars_stripped(self):
        srv = start_server({"status": 500, "ctype": "text/plain",
                            "body": b"oops\x1b[31m red\x07"})
        self.addCleanup(stop, srv)
        p = self.script("[a] hi\n")
        code, out, err = self.run_main(["send", p, "--voice-id", "v1", "--api-base", base_of(srv), "--out", self.fresh_out()],
                                       env={lv.KEY_ENV: KEY})
        self.assertNotIn("\x1b", err)
        self.assertNotIn("\x07", err)

    def test_exception_text_never_has_key(self):
        p = self.script("[a] hi\n")
        boom = OSError("connect failed for header " + KEY)
        with mock.patch.object(lv, "synthesize", side_effect=boom):
            code, out, err = self.run_main(["send", p, "--voice-id", "v1", "--out", self.fresh_out()],
                                           env={lv.KEY_ENV: KEY})
        self.assertEqual(code, 1)
        self.assertNotIn(KEY, out + err)

    def test_urlerror_reason_with_key_is_redacted(self):
        srv = start_server()
        self.addCleanup(stop, srv)
        p = self.script("[a] hi\n")
        err_obj = lv.urllib.error.URLError("refused " + KEY)
        with mock.patch.object(lv.urllib.request.OpenerDirector, "open", side_effect=err_obj):
            code, out, err = self.run_main(["send", p, "--voice-id", "v1", "--api-base", base_of(srv),
                                            "--out", self.fresh_out()],
                                           env={lv.KEY_ENV: KEY})
        self.assertEqual(code, 1)
        self.assertNotIn(KEY, out + err)

    def test_invalid_key_value_not_echoed(self):
        bad = "has space " + KEY
        p = self.script("[a] hi\n")
        code, out, err = self.run_main(["send", p, "--voice-id", "v1", "--out", self.fresh_out()], env={lv.KEY_ENV: bad})
        self.assertEqual(code, 1)
        self.assertNotIn(KEY, out + err)

    def test_newline_in_key_not_echoed_nor_sent(self):
        srv = start_server()
        self.addCleanup(stop, srv)
        bad = KEY + "\nX-Evil: 1"
        p = self.script("[a] hi\n")
        code, out, err = self.run_main(["send", p, "--voice-id", "v1", "--api-base", base_of(srv), "--out", self.fresh_out()],
                                       env={lv.KEY_ENV: bad})
        self.assertEqual(code, 1)
        self.assertEqual(srv.requests, [])
        self.assertNotIn(KEY, out + err)

    def test_redirect_to_other_listener_refused(self):
        target = start_server()
        self.addCleanup(stop, target)
        srv = start_server({"redirect": base_of(target) + "/v1/text-to-speech/v1"})
        self.addCleanup(stop, srv)
        p = self.script("[a] hi\n")
        code, out, err = self.run_main(["send", p, "--voice-id", "v1", "--api-base", base_of(srv), "--out", self.fresh_out()],
                                       env={lv.KEY_ENV: KEY})
        self.assertEqual(code, 1)
        self.assertIn("redirect", err)
        self.assertEqual(len(srv.requests), 1)
        self.assertEqual(target.requests, [])  # the key never reached the other host
        self.assertNotIn(KEY, out + err)

    def test_redirect_to_foreign_hostname_not_followed(self):
        srv = start_server({"redirect": "http://" + EVIL_HOST + "/x"})
        self.addCleanup(stop, srv)
        p = self.script("[a] hi\n")
        code, out, err = self.run_main(["send", p, "--voice-id", "v1", "--api-base", base_of(srv), "--out", self.fresh_out()],
                                       env={lv.KEY_ENV: KEY})
        self.assertEqual(code, 1)
        self.assertIn("redirect", err)

    def test_content_type_must_be_audio_mpeg(self):
        srv = start_server({"ctype": "text/html", "body": b"<html>"})
        self.addCleanup(stop, srv)
        p = self.script("[a] hi\n")
        o = self.fresh_out()
        code, out, err = self.run_main(["send", p, "--voice-id", "v1", "--api-base", base_of(srv), "--out", o],
                                       env={lv.KEY_ENV: KEY})
        self.assertEqual(code, 1)
        self.assertIn("content type", err)
        self.assertEqual(os.listdir(o), [])

    def test_content_type_with_params_accepted(self):
        srv = start_server({"ctype": "audio/mpeg; charset=binary"})
        self.addCleanup(stop, srv)
        p = self.script("[a] hi\n")
        code, out, err = self.run_main(["send", p, "--voice-id", "v1", "--api-base", base_of(srv), "--out", self.fresh_out()],
                                       env={lv.KEY_ENV: KEY})
        self.assertEqual(code, 0, err)

    def test_response_size_cap(self):
        srv = start_server({"body": b"\x00" * 5000})
        self.addCleanup(stop, srv)
        p = self.script("[a] hi\n")
        o = self.fresh_out()
        with mock.patch.object(lv, "MAX_RESPONSE_BYTES", 1000):
            code, out, err = self.run_main(["send", p, "--voice-id", "v1", "--api-base", base_of(srv), "--out", o],
                                           env={lv.KEY_ENV: KEY})
        self.assertEqual(code, 1)
        self.assertIn("size cap", err)
        self.assertEqual(os.listdir(o), [])

    def test_empty_audio_rejected(self):
        srv = start_server({"body": b""})
        self.addCleanup(stop, srv)
        p = self.script("[a] hi\n")
        code, out, err = self.run_main(["send", p, "--voice-id", "v1", "--api-base", base_of(srv), "--out", self.fresh_out()],
                                       env={lv.KEY_ENV: KEY})
        self.assertEqual(code, 1)

    def test_second_request_failure_reports_partial(self):
        srv = start_server()
        self.addCleanup(stop, srv)
        p = self.script(TAGGED)
        real = lv.synthesize
        calls = []

        def flaky(*a, **k):
            calls.append(1)
            if len(calls) == 2:
                raise lv.ToolError("API error HTTP 429")
            return real(*a, **k)
        with mock.patch.object(lv, "synthesize", flaky):
            code, out, err = self.run_main(["send", p, "--voice-id", "v1", "--api-base", base_of(srv), "--out", self.fresh_out()],
                                           env={lv.KEY_ENV: KEY})
        self.assertEqual(code, 1)
        self.assertIn("1 file(s) were written", err)

    def test_proxy_env_ignored_for_loopback(self):
        srv = start_server()
        self.addCleanup(stop, srv)
        p = self.script("[a] hi\n")
        code, out, err = self.run_main(["send", p, "--voice-id", "v1", "--api-base", base_of(srv), "--out", self.fresh_out()],
                                       env={lv.KEY_ENV: KEY, "HTTP_PROXY": "http://127.0.0.1:9",
                                            "http_proxy": "http://127.0.0.1:9"})
        self.assertEqual(code, 0, err)


class Hosts(Base):
    def test_allowed_bases(self):
        self.assertEqual(lv.check_api_base("https://api.elevenlabs.io"), "https://api.elevenlabs.io")
        self.assertEqual(lv.check_api_base("https://api.elevenlabs.io/"), "https://api.elevenlabs.io")
        self.assertEqual(lv.check_api_base("http://127.0.0.1:8123"), "http://127.0.0.1:8123")

    def test_refused_bases(self):
        bad = [
            "http://api.elevenlabs.io",
            "https://api.elevenlabs.io." + "evil.tld",
            "https://evil.tld",
            "https://api.elevenlabs.io" + "@" + EVIL_HOST,
            "https://user:pw" + "@" + "api.elevenlabs.io",
            "https://api.elevenlabs.io:444",
            "https://api.elevenlabs.io/v1",
            "https://api.elevenlabs.io?x=1",
            "https://api.elevenlabs.io\\" + EVIL_HOST,
            "http://localhost:8000",
            "http://127.0.0.1",
            "http://127.0.0.1:0",
            "http://127.0.0.1:99999",
            "http://127.0.0.1:80/x",
            "http://127.0.0.2:80",
            "http://[::1]:80",
            "ftp://127.0.0.1:21",
            "https://127.0.0.1:443",
            "",
            "https://api.elevenlabs.io\n.evil",
        ]
        for b in bad:
            with self.assertRaises(lv.Refusal, msg=b):
                lv.check_api_base(b)

    def test_bad_host_exit_2_and_no_key_read(self):
        p = self.script("[a] hi\n")
        with mock.patch.object(lv, "read_key", side_effect=AssertionError("key read")):
            code, out, err = self.run_main(["send", p, "--voice-id", "v1", "--api-base", "https://" + EVIL_HOST,
                                            "--out", self.fresh_out()], env={lv.KEY_ENV: KEY})
        self.assertEqual(code, 2)

    def test_bad_host_in_dry_run_also_refused(self):
        p = self.script("[a] hi\n")
        code, out, err = self.run_main(["dry", p, "--voice-id", "v1", "--api-base", "https://" + EVIL_HOST])
        self.assertEqual(code, 2)

    def test_voice_id_path_traversal_refused(self):
        p = self.script("[a] hi\n")
        for v in ["../x", "a/b", "a?b", "a b", "", "x" * 65]:
            code, out, err = self.run_main(["dry", p, "--voice-id", v, "--out", self.fresh_out()])
            self.assertIn(code, (1, 2), v)
            self.assertNotEqual(code, 0, v)

    def test_model_charset_refused(self):
        p = self.script("[a] hi\n")
        code, out, err = self.run_main(["dry", p, "--voice-id", "v1", "--model", "a/../b"])
        self.assertEqual(code, 2)


class Refusals(Base):
    def test_cap_refused_even_with_send(self):
        srv = start_server()
        self.addCleanup(stop, srv)
        p = self.script("[a] " + "word " * 50 + "\n")
        code, out, err = self.run_main(["send", p, "--voice-id", "v1", "--max-chars", "100", "--api-base", base_of(srv), "--out", self.fresh_out()],
                                       env={lv.KEY_ENV: KEY})
        self.assertEqual(code, 2)
        self.assertEqual(srv.requests, [])

    def test_cap_refused_in_dry_run(self):
        p = self.script("[a] " + "word " * 50 + "\n")
        code, out, err = self.run_main(["dry", p, "--voice-id", "v1", "--max-chars", "100"])
        self.assertEqual(code, 2)
        self.assertIn("max-chars", err)

    def test_default_cap_is_5000(self):
        p = self.script("[a] " + "a" * 5001 + "\n")
        code, out, err = self.run_main(["dry", p, "--voice-id", "v1", "--out", self.fresh_out()])
        self.assertEqual(code, 2)

    def test_cap_exact_is_ok(self):
        p = self.script("[a] " + "a" * 100 + "\n")
        code, out, err = self.run_main(["dry", p, "--voice-id", "v1", "--max-chars", "100",
                                        "--out", self.fresh_out()])
        self.assertEqual(code, 0, err)

    def test_existing_nonempty_out_refused(self):
        p = self.script("[a] hi\n")
        o = self.fresh_out()
        os.makedirs(o)
        with open(o + "/keep.txt", "w") as f:
            f.write("x")
        code, out, err = self.run_main(["dry", p, "--voice-id", "v1", "--out", o])
        self.assertEqual(code, 2)

    def test_existing_empty_out_ok(self):
        p = self.script("[a] hi\n")
        o = self.fresh_out()
        os.makedirs(o)
        code, out, err = self.run_main(["dry", p, "--voice-id", "v1", "--out", o])
        self.assertEqual(code, 0, err)

    def test_existing_file_as_out_refused(self):
        p = self.script("[a] hi\n")
        code, out, err = self.run_main(["dry", p, "--voice-id", "v1", "--out", p])
        self.assertEqual(code, 2)

    def test_existing_mp3_never_overwritten(self):
        srv = start_server()
        self.addCleanup(stop, srv)
        p = self.script("[a] hi\n")
        o = self.fresh_out() + ".mp3"
        with open(o, "wb") as f:
            f.write(b"mine")
        code, out, err = self.run_main(["send", p, "--voice-id", "v1", "--api-base", base_of(srv), "--out", o],
                                       env={lv.KEY_ENV: KEY})
        self.assertEqual(code, 2)
        with open(o, "rb") as f:
            self.assertEqual(f.read(), b"mine")
        self.assertEqual(srv.requests, [])

    def test_mp3_out_with_many_requests_refused(self):
        p = self.script(TAGGED)
        code, out, err = self.run_main(["dry", p, "--voice-id", "v1", "--out", self.fresh_out() + ".mp3"])
        self.assertEqual(code, 2)

    def test_argv_key_options_refused(self):
        p = self.script("[a] hi\n")
        for opt in ["--api-key", "--api-key=" + KEY, "--apikey", "--key", "--token=" + KEY,
                    "--xi-api-key", "--secret", "--password"]:
            code, out, err = self.run_main(["dry", p, "--voice-id", "v1", opt, KEY])
            self.assertEqual(code, 2, opt)
            self.assertNotIn(KEY, out + err, opt)
            self.assertIn(lv.KEY_ENV, err)

    def test_argv_key_refused_with_send_and_env(self):
        srv = start_server()
        self.addCleanup(stop, srv)
        p = self.script("[a] hi\n")
        code, out, err = self.run_main(["send", p, "--voice-id", "v1", "--api-key", KEY,
                                        "--api-base", base_of(srv)], env={lv.KEY_ENV: KEY})
        self.assertEqual(code, 2)
        self.assertEqual(srv.requests, [])

    def test_unknown_option_does_not_echo_value(self):
        p = self.script("[a] hi\n")
        code, out, err = self.run_main(["dry", p, "--voice-id", "v1", "--bogus-" + KEY])
        self.assertEqual(code, 1)
        self.assertNotIn(KEY, out + err)

    def test_abbreviations_not_accepted(self):
        p = self.script("[a] hi\n")
        code, out, err = self.run_main(["dry", p, "--voice-id", "v1", "--max", "10"])
        self.assertEqual(code, 1)

    def test_tagged_and_all_conflict(self):
        p = self.script("[a] hi\n")
        code, out, err = self.run_main(["dry", p, "--voice-id", "v1", "--tagged-only", "--all-lines"])
        self.assertEqual(code, 1)

    def test_missing_script_exit_1(self):
        code, out, err = self.run_main(["dry", self.tmp + "/nope.md", "--voice-id", "v1"])
        self.assertEqual(code, 1)

    def test_no_spoken_lines_exit_1(self):
        p = self.script("# only heading\nplain\n")
        code, out, err = self.run_main(["dry", p, "--voice-id", "v1", "--tagged-only"])
        self.assertEqual(code, 1)


class Text(Base):
    def lines(self, text, mode="auto", tag=lv.DEFAULT_TAG):
        return lv.spoken_lines(text, mode, lv.compile_tag(tag))

    def test_arabic_preserved(self):
        t = "[راوي] أبي أروح السوق الحين\n"
        self.assertEqual(self.lines(t), ["أبي أروح السوق الحين"])

    def test_arabic_in_dry_run_and_utf8_cli(self):
        p = self.script("[راوي] مرحبا بكم\n")
        r = subprocess.run([sys.executable, "-X", "utf8", SCRIPT, "dry", p, "--voice-id", "v1",
                            "--out", self.fresh_out()], capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("مرحبا بكم".encode("utf-8"), r.stdout)

    def test_crlf_and_bom(self):
        raw = b"\xef\xbb\xbf[a] one\r\n[b] two\r\n"
        p = self.script("", raw=raw)
        self.assertEqual(self.lines(lv.read_script(p)), ["one", "two"])

    def test_cr_only_lines(self):
        self.assertEqual(self.lines("[a] one\r[a] two"), ["one", "two"])

    def test_invalid_utf8_refused(self):
        p = self.script("", raw=b"[a] \xff\xfe bad")
        code, out, err = self.run_main(["dry", p, "--voice-id", "v1"])
        self.assertEqual(code, 1)

    def test_auto_prefers_tagged(self):
        self.assertEqual(self.lines(TAGGED), ["Hello there.", "Second line."])

    def test_auto_falls_back_to_all(self):
        self.assertEqual(self.lines("one\n\ntwo\n"), ["one", "two"])

    def test_tagged_only(self):
        self.assertEqual(self.lines(TAGGED, "tagged"), ["Hello there.", "Second line."])

    def test_all_lines_strips_tags_keeps_order(self):
        self.assertEqual(self.lines("intro\n[a] two\nthree", "all"), ["intro", "two", "three"])

    def test_custom_tag(self):
        self.assertEqual(self.lines("NARR: hi\nother\n", "tagged", r"^NARR:"), ["hi"])

    def test_tag_matching_empty_is_not_a_tag(self):
        self.assertEqual(self.lines("hello\n", "tagged", r"^\s*"), [])

    def test_empty_after_tag_skipped(self):
        self.assertEqual(self.lines("[a]\n[b]   \n[c] x"), ["x"])

    def test_bad_tag_regex(self):
        with self.assertRaises(lv.ToolError):
            lv.compile_tag("([")
        with self.assertRaises(lv.ToolError):
            lv.compile_tag("a" * 301)

    def test_control_chars_stripped(self):
        s = "[a] he\x1b[31mllo\x00 wor\x07ld\tx y"
        self.assertEqual(self.lines(s), ["he[31mllo world x"])

    def test_bidi_and_zero_width_stripped(self):
        s = "[a] a‮b​c⁦d⁩e﻿‏"
        self.assertEqual(self.lines(s), ["abcde"])

    def test_stripped_text_is_what_is_sent(self):
        srv = start_server()
        self.addCleanup(stop, srv)
        p = self.script("[a] he‮llo\x07\n")
        self.run_main(["send", p, "--voice-id", "v1", "--api-base", base_of(srv),
                       "--out", self.fresh_out()], env={lv.KEY_ENV: KEY})
        self.assertEqual(json.loads(srv.requests[0]["body"].decode("utf-8"))["text"], "hello")

    def test_script_text_is_data_not_options(self):
        p = self.script("[a] --send --api-base https://" + EVIL_HOST + " --voice-id x\n")
        code, out, err = self.run_main(["dry", p, "--voice-id", "v1", "--out", self.fresh_out()])
        self.assertEqual(code, 0, err)
        self.assertIn("nothing was sent", out)

    def test_dry_run_preview_has_no_control_chars(self):
        p = self.script("[a] hi\x1b[2Jthere\n")
        code, out, err = self.run_main(["dry", p, "--voice-id", "v1", "--out", self.fresh_out()])
        self.assertNotIn("\x1b", out)

    def test_long_line_is_split_under_limit(self):
        sentence = "This is a sentence. "
        text = "[a] " + sentence * 300 + "\n"  # 6000 chars
        pieces = lv.split_long(lv.clean_text(text[4:]))
        self.assertTrue(all(len(x) <= lv.CHUNK_LIMIT for x in pieces))
        self.assertEqual(sum(len(x) for x in pieces) > 5000, True)
        self.assertGreaterEqual(len(pieces), 3)

    def test_long_unbroken_line_hard_cut(self):
        pieces = lv.split_long("x" * 6000)
        self.assertEqual([len(x) for x in pieces], [2500, 2500, 1000])

    def test_long_line_makes_several_requests(self):
        p = self.script("[a] " + ("Sentence number one. " * 200) + "\n")
        code, out, err = self.run_main(["dry", p, "--voice-id", "v1", "--out", self.fresh_out()])
        self.assertEqual(code, 0, err)
        self.assertIn("spoken lines: 1", out)
        self.assertIn("requests: 2", out)


class Cli(Base):
    def test_subprocess_dry_run(self):
        p = self.script(TAGGED)
        r = subprocess.run([sys.executable, SCRIPT, "dry", p, "--voice-id", "v1",
                            "--out", self.fresh_out()], capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(b"nothing was sent", r.stdout)

    def test_subprocess_argv_key_refused_exit_2(self):
        p = self.script(TAGGED)
        r = subprocess.run([sys.executable, SCRIPT, "dry", p, "--voice-id", "v1",
                            "--api-key", KEY], capture_output=True)
        self.assertEqual(r.returncode, 2)
        self.assertNotIn(KEY.encode(), r.stdout + r.stderr)

    def test_subprocess_send_without_key_exit_1(self):
        p = self.script(TAGGED)
        env = {k: v for k, v in os.environ.items() if k != lv.KEY_ENV}
        r = subprocess.run([sys.executable, SCRIPT, "send", p, "--voice-id", "v1",
                            "--out", self.fresh_out()], capture_output=True, env=env)
        self.assertEqual(r.returncode, 1)

    def test_source_never_reads_files_for_key(self):
        with open(SCRIPT, encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("dotenv", src)
        self.assertNotIn(".env.", src)
        self.assertEqual(src.count("os.environ"), 1)
        self.assertEqual(src.count("os.environ.get(KEY_ENV"), 1)

    def test_manifest_valid(self):
        with open(ROOT + "/.claude-plugin/plugin.json", encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(d["name"], "lahja-voice")
        self.assertEqual(d["license"], "MIT")
        self.assertEqual(d["version"], "0.1.3")

    def _skill(self):
        with open(ROOT + "/skills/lahja-voice/SKILL.md", encoding="utf-8") as f:
            return f.read()

    def test_skill_allowed_tools_dry_only(self):
        line = [l for l in self._skill().splitlines() if l.startswith("allowed-tools:")]
        self.assertEqual(len(line), 1)
        self.assertEqual(line[0].strip(),
                         "allowed-tools: Bash(python ${CLAUDE_PLUGIN_ROOT}/scripts/lahja_voice.py dry *)")
        self.assertIn("dry *", line[0])
        self.assertNotIn("send", line[0])

    def test_skill_text_matches_gate(self):
        s = self._skill()
        self.assertIn("python ${CLAUDE_PLUGIN_ROOT}/scripts/lahja_voice.py dry ", s)
        self.assertIn("python ${CLAUDE_PLUGIN_ROOT}/scripts/lahja_voice.py send ", s)
        self.assertIn("NOT pre-approved", s)
        self.assertIn("permission prompt", s)
        self.assertNotIn("--send", s)

    def test_send_flag_rejected_with_clear_message(self):
        p = self.script("[a] hi\n")
        for mode in ("dry", "send"):
            code, out, err = self.run_main([mode, p, "--voice-id", "v1", "--send"],
                                           env={lv.KEY_ENV: KEY})
            self.assertEqual(code, 1)
            self.assertIn("use the send subcommand", err)

    def test_subcommand_required(self):
        p = self.script("[a] hi\n")
        for argv in ([], [p, "--voice-id", "v1"], ["go", p, "--voice-id", "v1"]):
            code, out, err = self.run_main(argv)
            self.assertEqual(code, 1)
            self.assertIn("subcommand", err)

    def test_dry_subcommand_never_reads_key_or_network(self):
        p = self.script("[a] hi\n")

        def boom(*a, **k):
            raise AssertionError("touched")
        with mock.patch.object(lv, "read_key", boom), mock.patch.object(socket, "socket", boom):
            code, out, err = self.run_main(["dry", p, "--voice-id", "v1", "--out", self.fresh_out()],
                                           env={lv.KEY_ENV: KEY})
        self.assertEqual(code, 0, err)


SKKEY = "sk" + "_" + "a1b2c3d4" * 3


class Fix011(Base):
    def send(self, srv, text="[a] hi\n", extra=(), env=None, name="o"):
        p = self.script(text)
        o = self.fresh_out(name)
        argv = ["send", p, "--voice-id", "v1", "--api-base", base_of(srv), "--out", o] + list(extra)
        code, out, err = self.run_main(argv, env=env or {lv.KEY_ENV: KEY})
        return code, out, err, o

    def serve(self, beh):
        srv = start_server(beh)
        self.addCleanup(stop, srv)
        return srv

    # F1
    def test_truncated_body_is_an_error(self):
        srv = self.serve({"cl": "1000"})
        code, out, err, o = self.send(srv)
        self.assertEqual(code, 1)
        self.assertIn("incomplete", err)
        self.assertEqual(os.listdir(o), [])

    def test_truncated_chunked_body_is_an_error(self):
        srv = self.serve({"cl": None, "chunked": True, "raw": b"40\r\n" + MP3[:10]})
        code, out, err, o = self.send(srv)
        self.assertEqual(code, 1)
        self.assertEqual(os.listdir(o), [])

    def test_no_length_and_not_chunked_refused(self):
        srv = self.serve({"cl": None})
        code, out, err, o = self.send(srv)
        self.assertEqual(code, 1)
        self.assertIn("completeness", err)
        self.assertEqual(os.listdir(o), [])

    def test_complete_chunked_body_ok(self):
        raw = b"%x\r\n" % len(MP3) + MP3 + b"\r\n0\r\n\r\n"
        srv = self.serve({"cl": None, "chunked": True, "raw": raw})
        code, out, err, o = self.send(srv)
        self.assertEqual(code, 0, err)

    # F10
    def test_html_labelled_audio_refused(self):
        srv = self.serve({"body": b"<html>not audio</html>"})
        code, out, err, o = self.send(srv)
        self.assertEqual(code, 1)
        self.assertIn("not MP3", err)
        self.assertEqual(os.listdir(o), [])

    def test_json_labelled_audio_refused(self):
        srv = self.serve({"body": b'{"detail":"x"}'})
        self.assertEqual(self.send(srv)[0], 1)

    def test_mpeg_frame_sync_accepted(self):
        srv = self.serve({"body": b"\xff\xfb\x90\x00" + b"\x00" * 40})
        self.assertEqual(self.send(srv)[0], 0)

    # F4
    def test_failed_write_keeps_recovery_file(self):
        srv = self.serve({})
        rec = self.tmp + "/rec"
        os.makedirs(rec)
        real = lv.tempfile.mkstemp

        def picky(*a, **k):
            if k.get("dir"):
                raise PermissionError("denied")
            return real(*a, **k)
        with mock.patch.object(lv.tempfile, "tempdir", rec), \
                mock.patch.object(lv.tempfile, "mkstemp", picky):
            code, out, err, o = self.send(srv)
        self.assertEqual(code, 1)
        self.assertIn("was kept in", err)
        files = os.listdir(rec)
        self.assertEqual(len(files), 1)
        with open(rec + "/" + files[0], "rb") as f:
            self.assertEqual(f.read(), MP3)

    def test_no_part_files_left_after_success(self):
        srv = self.serve({})
        code, out, err, o = self.send(srv, TAGGED)
        self.assertEqual(sorted(os.listdir(o)), ["line_001.mp3", "line_002.mp3"])

    def test_out_names_refused_up_front(self):
        srv = self.serve({})
        p = self.script("[a] hi\n")
        bad = [self.tmp + "/ts.mp3 ", self.tmp + "/td.mp3.", self.tmp + "/CON",
               self.tmp + "/x/NUL.mp3", self.tmp + "/f.txt:x.mp3", "",
               "//host/share/x", "\\\\host\\share\\x", self.tmp + "/a\nb",
               self.tmp + "/a\x1bb", self.tmp + "/a\u202eb"]
        for o in bad:
            code, out, err = self.run_main(["send", p, "--voice-id", "v1", "--api-base",
                                            base_of(srv), "--out", o], env={lv.KEY_ENV: KEY})
            self.assertEqual(code, 2, repr(o))
            self.assertNotIn("\x1b", err)
            self.assertNotIn("host", err.replace("--out", ""))
        self.assertEqual(srv.requests, [])

    # F2 / F3 / F14
    def test_dry_refuses_directory_and_device(self):
        dev = "NUL" if os.name == "nt" else "/dev/null"
        for path in (self.tmp, dev):
            code, out, err = self.run_main(["dry", path, "--voice-id", "v1"])
            self.assertEqual(code, 1, path)
            self.assertEqual(out, "")

    def test_unc_and_stream_paths_refused_before_filesystem(self):
        def boom(*a, **k):
            raise AssertionError("filesystem touched")
        real = self.script("[a] hi\n")
        for bad in ("//127.0.0.1/x/s.md", "\\\\127.0.0.1\\x\\s.md", "//./NUL", self.tmp + "/s.md:x"):
            # A path that does not exist would give exit 1 (not found) if the filesystem were
            # consulted first; exit 2 proves the refusal happens before any filesystem call.
            code, out, err = self.run_main(["dry", bad, "--voice-id", "v1"])
            self.assertEqual(code, 2, bad)
            code, out, err = self.run_main(["dry", real, "--voice-id", "v1", "--out", bad])
            self.assertEqual(code, 2, bad)

    def test_dry_preview_is_capped(self):
        p = self.script("".join("[a] line %d\n" % i for i in range(120)))
        code, out, err = self.run_main(["dry", p, "--voice-id", "v1", "--out", self.fresh_out()])
        self.assertEqual(code, 0, err)
        self.assertIn("requests: 120", out)
        self.assertIn("050 [", out)
        self.assertNotIn("051 [", out)
        self.assertIn("70 more request(s) not shown", out)

    def test_script_read_is_byte_capped(self):
        p = self.script("[a] " + "x" * 100 + "\n")
        with mock.patch.object(lv, "MAX_SCRIPT_BYTES", 50):
            code, out, err = self.run_main(["dry", p, "--voice-id", "v1"])
        self.assertEqual(code, 1)
        self.assertIn("larger", err)

    def test_out_existence_message_does_not_echo_path(self):
        p = self.script("[a] hi\n")
        secret = self.tmp + "/some_private_name.txt"
        with open(secret, "w") as f:
            f.write("x")
        code, out, err = self.run_main(["dry", p, "--voice-id", "v1", "--out", secret])
        self.assertEqual(code, 2)
        self.assertNotIn("some_private_name", err + out)

    # F5 / F6
    def test_redact_cut_off_key(self):
        text = " " * 50 + "X" + KEY[:9]
        r = lv.redact(text, KEY)
        self.assertNotIn(KEY[:6], r)
        self.assertIn("[redacted]", r)
        self.assertEqual(lv.redact("nothing here", KEY), "nothing here")

    def test_key_straddling_error_read_limit_hidden(self):
        body = (b" " * 1190) + b"X" + KEY.encode()
        srv = self.serve({"status": 401, "ctype": "text/plain", "body": body})
        code, out, err, o = self.send(srv)
        self.assertEqual(code, 1)
        self.assertNotIn(KEY[:6], out + err)

    def test_key_in_other_arguments_refused_on_send(self):
        srv = self.serve({})
        p = self.script("[a] hi\n")
        base = ["send", p, "--api-base", base_of(srv)]
        cases = [base + ["--voice-id", KEY, "--out", self.fresh_out()],
                 base + ["--voice-id", "v1", "--model", KEY, "--out", self.fresh_out()],
                 base + ["--voice-id", "v1", "--out", self.tmp + "/" + KEY],
                 base + ["--voice-id", "v1", "--tag", KEY, "--out", self.fresh_out()]]
        for argv in cases:
            code, out, err = self.run_main(argv, env={lv.KEY_ENV: KEY})
            self.assertEqual(code, 2, argv)
            self.assertNotIn(KEY, out + err)
        self.assertEqual(srv.requests, [])

    def test_key_shaped_argument_refused_without_echo(self):
        for argv in (["dry", SKKEY, "--voice-id", "v1"], ["dry", "x.md", "--voice-id", SKKEY]):
            code, out, err = self.run_main(argv)
            self.assertEqual(code, 2)
            self.assertNotIn(SKKEY, out + err)

    # F7 / F8
    def test_dangerous_tags_refused_fast(self):
        p = self.script("a" * 41 + "\n")
        for tag in ("(a+)+$", "(a|aa)+b", "a*a*a*a*b", "(a)\\1", "(?=a)b", "a{99999999999}",
                    "[[:alpha:]]", "(?P<n>a)", "(.*)*"):
            t0 = time.time()
            code, out, err = self.run_main(["dry", p, "--voice-id", "v1", "--tag", tag])
            self.assertEqual(code, 1, tag)
            self.assertLess(time.time() - t0, 3, tag)
            self.assertNotIn("Traceback", err)

    def test_reasonable_tags_accepted(self):
        for tag in (lv.DEFAULT_TAG, r"^NARR:", r"^\(\w+\)", r"^(?:A|B):", r"(?i)^\[x\]", r"^\w{1,10}:"):
            lv.compile_tag(tag)

    def test_tag_only_sees_window(self):
        tag = lv.compile_tag(lv.DEFAULT_TAG)
        long_line = "[" + "x" * 400 + "] hi"
        self.assertEqual(lv.spoken_lines(long_line, "tagged", tag), [])

    def test_tag_budget_enforced(self):
        tag = lv.compile_tag(lv.DEFAULT_TAG)
        with mock.patch.object(lv, "TAG_BUDGET", -1):
            with self.assertRaises(lv.ToolError):
                lv.spoken_lines("a\nb\n", "all", tag)

    def test_locked_script_is_clean_error(self):
        p = self.script("[a] hi\n")
        with mock.patch.object(lv, "open", create=True, side_effect=PermissionError("locked")):
            for mode in ("dry", "send"):
                code, out, err = self.run_main([mode, p, "--voice-id", "v1"], env={lv.KEY_ENV: KEY})
                self.assertEqual(code, 1)
                self.assertIn("PermissionError", err)
                self.assertNotIn("Traceback", err)

    def test_unexpected_exception_has_no_traceback(self):
        p = self.script("[a] hi\n")
        with mock.patch.object(lv, "build_plan", side_effect=RuntimeError("secret detail")):
            code, out, err = self.run_main(["dry", p, "--voice-id", "v1"])
        self.assertEqual(code, 1)
        self.assertIn("RuntimeError", err)
        self.assertNotIn("secret detail", err)

    # F9
    def test_dry_out_cannot_forge_output(self):
        p = self.script("[a] hi\n")
        code, out, err = self.run_main(["dry", p, "--voice-id", "v1", "--out",
                                        "x\ncharacters: 1\n\x1b[2J\u202eDRY RUN ok"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotIn("\x1b", err)

    # F12
    def test_unicode_line_breaks_split_lines(self):
        for br in ("\u2028", "\u2029", "\x85", "\x0b", "\x0c"):
            t = "[a] hello%s[b] world" % br
            self.assertEqual(lv.spoken_lines(t, "auto", lv.compile_tag(lv.DEFAULT_TAG)),
                             ["hello", "world"], repr(br))
        self.assertEqual(lv.spoken_lines("one\x0btwo\x0cthree", "all", lv.compile_tag(lv.DEFAULT_TAG)),
                         ["one", "two", "three"])

    # F11
    def test_total_deadline_per_request(self):
        srv = self.serve({"cl": "1000", "drip": 8})
        t0 = time.time()
        with mock.patch.object(lv, "TOTAL_TIMEOUT", 1):
            code, out, err, o = self.send(srv)
        self.assertEqual(code, 1)
        self.assertIn("total time limit", err)
        self.assertLess(time.time() - t0, 2.5)

    # F16
    def test_http_exception_keeps_written_count(self):
        srv = self.serve({})
        real = lv.synthesize
        calls = []

        def flaky(*a, **k):
            calls.append(1)
            if len(calls) == 2:
                raise lv.http.client.IncompleteRead(b"x")
            return real(*a, **k)
        with mock.patch.object(lv, "synthesize", flaky):
            code, out, err, o = self.send(srv, TAGGED)
        self.assertEqual(code, 1)
        self.assertIn("1 file(s) were written", err)

    # F18
    def test_ci_pattern_catches_elevenlabs_key_shape(self):
        with open(ROOT + "/.github/workflows/ci.yml", encoding="utf-8") as f:
            m = re.search(r"PATTERN='([^']*)'", f.read())
        self.assertTrue(m)
        pat = re.compile(m.group(1))
        self.assertTrue(pat.search("key = " + SKKEY))
        self.assertTrue(pat.search("=" + "sk" + "_" + "0123456789abcdef0123"))
        self.assertFalse(pat.search("sk" + "_" + "short"))
        self.assertFalse(pat.search("the sk_ prefix is documented"))

    # docs
    def test_docs_do_not_overclaim(self):
        texts = {}
        for name in ("README.md", "README.ar.md", "skills/lahja-voice/SKILL.md"):
            with open(ROOT + "/" + name, encoding="utf-8") as f:
                texts[name] = f.read()
        for name, t in texts.items():
            self.assertNotIn("enforced even with", t, name)
            self.assertNotIn("that host only", t, name)
            self.assertNotIn("never spoken", t, name)
        self.assertIn("127.0.0.1", texts["README.md"])
        self.assertIn("127.0.0.1", texts["skills/lahja-voice/SKILL.md"])
        self.assertIn("never made", texts["README.md"])


if __name__ == "__main__":
    unittest.main()
