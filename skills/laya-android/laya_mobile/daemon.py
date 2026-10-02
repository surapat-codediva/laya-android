"""Persistent Laya runtime: one local daemon keeps the checkpoint loaded between CLI calls.

Loading Laya takes ~10 s (torch import + weights) and its first inference is several times
slower than the next ones; a CLI process per command paid all of that every time. The daemon
loads each model once (lazily: `ml` and `en` only when first asked for), warms it up with one
tiny inference, and answers predict requests in ~0.2 s.

    client --(Unix socket, one JSON line each way)--> daemon (model loaded once)

Runtime files live in $LAYA_ANDROID_DATA_DIR/runtime (default ~/.laya-android/runtime), never in
the repository: laya.sock (mode 0600, local user only -- never a TCP port), laya.pid, laya.lock
(held by the live daemon: a second one cannot start), spawn.lock (serializes clients starting
it) and daemon.log. Requests are served one at a time (an inference lock), so several CLIs and
several devices can share one daemon safely. An idle daemon exits after DAEMON_IDLE_EXIT_S.

The client starts the daemon on demand. If it is unreachable or stops answering, the client
restarts it once and retries once; after that it raises DaemonError (CLI exit 2) -- never a
Laya "unsure" handoff.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time

from . import __version__, config
from .errors import LayaAndroidError
from .trajectory import data_dir

PROTOCOL = 1
MAX_MESSAGE = 8 * 1024 * 1024
SOCK_PATH_MAX = 100  # AF_UNIX paths are limited to ~104 bytes on macOS
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WARMUP_STATE = "app: warmup\nscreen: OK | Cancel"
WARMUP_QUESTIONS = {"q": {"type": "choice", "instructions": "Which button confirms?",
                          "criteria": {"0": 'button "OK"', "1": 'button "Cancel"'}},
                    "d": {"type": "noul", "instructions": "Is a dialog shown?"}}


class DaemonError(LayaAndroidError):
    """The Laya daemon could not be started or did not answer (exit 2, not a handoff)."""


class Unavailable(Exception):
    """Nothing usable at the socket: not running, crashed, or not answering in time."""


# ---------- paths ----------

def runtime_dir():
    return os.path.join(data_dir(), "runtime")


class Paths:
    def __init__(self, root=None):
        self.root = os.path.abspath(root or runtime_dir())
        self.pid = os.path.join(self.root, "laya.pid")
        self.lock = os.path.join(self.root, "laya.lock")
        self.spawn_lock = os.path.join(self.root, "spawn.lock")
        self.log = os.path.join(self.root, "daemon.log")
        sock = os.path.join(self.root, "laya.sock")
        if len(sock) > SOCK_PATH_MAX:
            # Too long for AF_UNIX: a short per-runtime-dir name in /tmp (still mode 0600).
            tag = hashlib.sha1(self.root.encode()).hexdigest()[:16]
            sock = os.path.join("/tmp" if os.path.isdir("/tmp") else self.root, "laya-android-%s.sock" % tag)
        self.sock = sock

    def ensure(self):
        os.makedirs(self.root, mode=0o700, exist_ok=True)


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pid(paths):
    try:
        with open(paths.pid) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def lock_held(paths):
    """True while a daemon process holds laya.lock (alive, maybe still starting)."""
    try:
        fd = os.open(paths.lock, os.O_RDWR | os.O_CREAT, 0o600)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
            return True
        raise
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def clean_stale(paths):
    """Remove the socket and pid file of a daemon that is gone. Only when no process holds the
    daemon lock, so a live daemon's files are never touched."""
    if lock_held(paths):
        return False
    for p in (paths.sock, paths.pid):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass
    return True


# ---------- protocol ----------

def _send(sock, obj):
    sock.sendall(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")


def _recv(sock):
    buf = bytearray()
    while not buf.endswith(b"\n"):
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
        if len(buf) > MAX_MESSAGE:
            raise ValueError("message too large")
    if not buf:
        raise ConnectionError("connection closed without a reply")
    return json.loads(buf.decode("utf-8"))


# ---------- server ----------

def load_model(name):
    from .decision import load_laya
    return load_laya(name)


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(30)  # a client that connects and says nothing cannot hold a thread forever
        try:
            req = _recv(self.request)
        except (OSError, ValueError, ConnectionError):
            return
        try:
            reply = self.server.daemon_obj.handle(req)
        except Exception as e:  # a bad request must not kill the daemon
            reply = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        try:
            _send(self.request, reply)
        except OSError:
            pass


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


class LayaDaemon:
    """The server side. `loader(name)` returns an object with Laya's predict(state, questions)."""

    def __init__(self, paths=None, loader=load_model, idle_exit=config.DAEMON_IDLE_EXIT_S, warmup=True):
        self.paths = paths or Paths()
        self.loader, self.idle_exit, self.warmup = loader, idle_exit, warmup
        self.models, self.model_info = {}, {}
        self.infer_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.started = time.time()
        self.last_request = time.monotonic()
        self.requests = 0
        self.predicts = 0
        self._lock_fd = None
        self._server = None

    # ---- lifecycle ----
    def acquire(self):
        """Take the daemon lock; False if another daemon holds it."""
        self.paths.ensure()
        fd = os.open(self.paths.lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._lock_fd = fd
        return True

    def bind(self):
        try:
            os.unlink(self.paths.sock)  # ours now: we hold the lock, so any old socket is stale
        except FileNotFoundError:
            pass
        old = os.umask(0o177)
        try:
            self._server = _Server(self.paths.sock, _Handler)
        finally:
            os.umask(old)
        os.chmod(self.paths.sock, 0o600)
        self._server.daemon_obj = self
        with open(self.paths.pid + ".tmp", "w") as f:
            f.write(str(os.getpid()))
        os.replace(self.paths.pid + ".tmp", self.paths.pid)

    def serve(self, install_signals=True):
        """Run until shutdown, SIGTERM/SIGINT or idle timeout; always cleans up."""
        if not self.acquire():
            raise DaemonError("another Laya daemon is already running (%s)" % self.paths.lock)
        try:
            self.bind()
            if install_signals:
                for sig in (signal.SIGTERM, signal.SIGINT):
                    signal.signal(sig, lambda *_: self.stop_event.set())
            t = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
            t.start()
            while not self.stop_event.wait(1.0):
                if self.idle_exit and time.monotonic() - self.last_request > self.idle_exit:
                    break
        finally:
            self.cleanup()

    def cleanup(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        for p in (self.paths.sock, self.paths.pid):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    # ---- requests ----
    def model(self, name):
        """The loaded model `name`, loading (and warming up) on first use. Call with infer_lock held."""
        if name not in ("ml", "en"):
            raise ValueError("unknown model %r" % name)
        if name not in self.models:
            t0 = time.perf_counter()
            m = self.loader(name)
            info = {"load_s": round(time.perf_counter() - t0, 2)}
            if self.warmup:
                t0 = time.perf_counter()
                m.predict(WARMUP_STATE, WARMUP_QUESTIONS)
                info["warmup_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            self.models[name], self.model_info[name] = m, info
        return self.models[name]

    def status(self, model=None):
        d = {"daemon": "running", "version": __version__, "pid": os.getpid(), "protocol": PROTOCOL, "socket": self.paths.sock,
             "uptime_s": round(time.time() - self.started, 1), "requests": self.requests, "predicts": self.predicts,
             "models_loaded": sorted(self.models), "models": self.model_info,
             "idle_exit_s": self.idle_exit}
        if model:
            d["model"], d["model_loaded"] = model, model in self.models
        return d

    def handle(self, req):
        self.last_request = time.monotonic()
        self.requests += 1
        op = req.get("op")
        if op == "ping":
            return {"ok": True, "pid": os.getpid(), "protocol": PROTOCOL}
        if op == "status":
            return {"ok": True, "result": self.status(req.get("model"))}
        if op == "shutdown":
            self.stop_event.set()
            return {"ok": True}
        if op == "load":
            with self.infer_lock:
                self.model(req["model"])
            return {"ok": True, "result": self.model_info[req["model"]]}
        if op == "predict":
            with self.infer_lock:  # one inference at a time: torch state is not shared across requests
                m = self.model(req["model"])
                t0 = time.perf_counter()
                out = m.predict(req["state"], req["questions"])
                ms = (time.perf_counter() - t0) * 1000
                self.predicts += 1
            return {"ok": True, "result": _jsonable(out), "ms": round(ms, 1)}
        return {"ok": False, "error": "unknown op %r" % op}


def _jsonable(x):
    """Laya answers may hold numpy/torch scalars."""
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, (str, int, float, bool)) or x is None:
        return x
    try:
        return float(x)
    except (TypeError, ValueError):
        return str(x)


# ---------- client ----------

def default_spawn_cmd(paths):
    return [sys.executable, "-m", "laya_mobile.daemon", "serve", "--runtime-dir", paths.root]


class DaemonClient:
    """Talks to the daemon, starting it when needed.

    timeout        one request once the model is loaded (LAYA_ANDROID_DAEMON_TIMEOUT, s)
    start_timeout  the daemon process accepting connections
    load_timeout   loading a model (the first time includes a ~650 MB download)
    """

    def __init__(self, paths=None, timeout=None, start_timeout=config.DAEMON_START_TIMEOUT_S,
                 load_timeout=config.MODEL_LOAD_TIMEOUT_S, spawn_cmd=None, env=None):
        self.paths = paths or Paths()
        self.timeout = timeout or config.daemon_timeout()
        self.start_timeout, self.load_timeout = start_timeout, load_timeout
        self.spawn_cmd = spawn_cmd or default_spawn_cmd(self.paths)
        self.env = env
        self.loaded = set()  # models this client already saw loaded
        self.checked = False  # the daemon answered once; later failures go through restart()
        self.started_daemon = False  # this client spawned it (startup latency is then part of its first call)
        self.restarts = 0

    # ---- transport ----
    def request(self, obj, timeout=None):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout or self.timeout)
        try:
            try:
                s.connect(self.paths.sock)
            except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError) as e:
                raise Unavailable("cannot connect to %s: %s" % (self.paths.sock, e)) from None
            try:
                _send(s, obj)
                return _recv(s)
            except socket.timeout:
                raise Unavailable("no reply within %ss" % (timeout or self.timeout)) from None
            except (OSError, ValueError, ConnectionError) as e:
                raise Unavailable("broken reply: %s" % e) from None
        finally:
            s.close()

    def ping(self, timeout=2.0):
        try:
            return self.request({"op": "ping"}, timeout).get("ok", False)
        except Unavailable:
            return False

    # ---- lifecycle ----
    def ensure_running(self):
        if not self.ping():
            self.start()

    def start(self):
        """Start the daemon unless one answers; wait until it accepts requests."""
        self.paths.ensure()
        with open(self.paths.spawn_lock, "a+") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)  # one client spawns, the others wait and then find it running
            try:
                if self.ping():
                    return False
                if not lock_held(self.paths):
                    clean_stale(self.paths)
                    self._spawn()
                self._wait_ready()
                return True
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)

    def _spawn(self):
        env = dict(os.environ if self.env is None else self.env)
        env["PYTHONPATH"] = SKILL_DIR + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env.setdefault("USE_TF", "0")
        with open(self.paths.log, "w") as log:
            subprocess.Popen(self.spawn_cmd, cwd=SKILL_DIR, env=env, stdin=subprocess.DEVNULL, stdout=log,
                             stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
        self.started_daemon = True
        self.loaded.clear()

    def _wait_ready(self):
        deadline = time.monotonic() + self.start_timeout
        while time.monotonic() < deadline:
            if self.ping(timeout=1.0):
                return
            time.sleep(0.05)
        raise DaemonError("Laya daemon did not become ready within %ss (log: %s)%s"
                          % (self.start_timeout, self.paths.log, self._log_tail()))

    def _log_tail(self):
        try:
            with open(self.paths.log, encoding="utf-8", errors="replace") as f:
                tail = f.read()[-600:].strip()
        except OSError:
            return ""
        return "\n" + tail if tail else ""

    def stop(self, timeout=10.0):
        """Ask the daemon to exit; SIGTERM it if it does not. True if one was running."""
        pid = read_pid(self.paths)
        try:
            self.request({"op": "shutdown"}, 2.0)
            asked = True
        except Unavailable:
            asked = False
        deadline = time.monotonic() + timeout
        while lock_held(self.paths) and time.monotonic() < deadline:
            if not asked and pid and _pid_alive(pid):
                os.kill(pid, signal.SIGTERM)
                asked = True
            time.sleep(0.05)
        if lock_held(self.paths) and pid and _pid_alive(pid):
            os.kill(pid, signal.SIGKILL)  # hung: it ignored shutdown and SIGTERM
            time.sleep(0.2)
        clean_stale(self.paths)
        self.loaded.clear()
        return asked

    def restart(self):
        self.restarts += 1
        self.stop(timeout=5.0)
        self.start()

    def status(self, model=None):
        try:
            r = self.request({"op": "status", "model": model}, 2.0)
        except Unavailable:
            d = {"daemon": "stopped", "socket": self.paths.sock}
            pid = read_pid(self.paths)
            if pid and lock_held(self.paths):
                d.update(daemon="not answering", pid=pid)
            return d
        return r.get("result", {})

    # ---- inference ----
    def _call(self, req, timeout):
        r = self.request(req, timeout)
        if not r.get("ok"):
            # The daemon answered: a request-level error, not a crash. No restart.
            raise DaemonError("Laya daemon error: %s" % r.get("error", "unknown"))
        return r

    def predict(self, model, state, questions):
        last = None
        for attempt in range(2):
            try:
                if not self.checked:
                    self.ensure_running()
                    self.checked = True
                if model not in self.loaded:
                    self._call({"op": "load", "model": model}, self.load_timeout)
                    self.loaded.add(model)
                return self._call({"op": "predict", "model": model, "state": state, "questions": questions},
                                  self.timeout)["result"]
            except Unavailable as e:
                last = e
                if attempt == 0:
                    self.restart()  # crashed or hung: restart once, retry once
        raise DaemonError("Laya daemon unavailable after one restart: %s (log: %s)%s"
                          % (last, self.paths.log, self._log_tail()))


# ---------- entry point: python -m laya_mobile.daemon serve ----------

def _load_dotted(spec):
    mod, _, fn = spec.partition(":")
    __import__(mod)
    return getattr(sys.modules[mod], fn)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="laya_mobile.daemon", description="Run the Laya daemon in the foreground.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--runtime-dir")
    s.add_argument("--idle-exit", type=float, default=config.DAEMON_IDLE_EXIT_S)
    s.add_argument("--no-warmup", action="store_true")
    s.add_argument("--loader", help=argparse.SUPPRESS)  # module:function, for tests (a fake model)
    a = ap.parse_args(argv)
    os.environ.setdefault("USE_TF", "0")
    loader = _load_dotted(a.loader) if a.loader else load_model
    d = LayaDaemon(Paths(a.runtime_dir), loader, a.idle_exit, not a.no_warmup)
    try:
        d.serve()
    except DaemonError as e:
        print("laya-android daemon: %s" % e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
