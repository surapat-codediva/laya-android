"""The persistent Laya daemon with a fake model: in-thread for protocol tests, a real spawned
process for lifecycle tests (start on demand, duplicates, stale files, crash, signals)."""
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

import fake_laya
from laya_mobile import daemon as dm
from laya_mobile.daemon import DaemonClient, DaemonError, LayaDaemon, Paths, clean_stale, lock_held, read_pid
from laya_mobile.decision import DaemonDecisionClient, decide

HERE = os.path.dirname(os.path.abspath(__file__))
Q = {"op": {"type": "choice", "instructions": "next?", "criteria": {"CLICK": "tap", "BACK": "back"}},
     "done": {"type": "noul", "instructions": "done?"}}


@pytest.fixture
def paths(tmp_path):
    return Paths(str(tmp_path / "runtime"))


@pytest.fixture
def served(paths):
    """A daemon serving in a thread, with the fake model; yields (daemon, client)."""
    loads = []

    def loader(name):
        loads.append(name)
        return fake_laya.FakeLaya(name)
    d = LayaDaemon(paths, loader, idle_exit=0)
    t = threading.Thread(target=d.serve, kwargs={"install_signals": False}, daemon=True)
    t.start()
    c = DaemonClient(paths, spawn_cmd=[sys.executable, "-c", "raise SystemExit('must not spawn')"], start_timeout=2)
    deadline = time.time() + 5
    while not c.ping() and time.time() < deadline:
        time.sleep(0.01)
    d.loads = loads
    yield d, c
    d.stop_event.set()
    t.join(5)


def spawning_client(paths, tmp_path, **kw):
    env = dict(os.environ, PYTHONPATH=HERE, FAKE_LAYA_LOADS=str(tmp_path / "loads.txt"))
    env.update(kw.pop("env", {}))
    cmd = [sys.executable, "-m", "laya_mobile.daemon", "serve", "--runtime-dir", paths.root, "--loader",
           "fake_laya:load"]
    return DaemonClient(paths, spawn_cmd=cmd, env=env, start_timeout=kw.pop("start_timeout", 15), **kw)


@pytest.fixture
def spawned(paths, tmp_path):
    clients = []

    def make(**kw):
        c = spawning_client(paths, tmp_path, **kw)
        clients.append(c)
        return c
    yield make
    for c in clients:
        c.stop(timeout=5)


def loads(tmp_path):
    try:
        with open(tmp_path / "loads.txt") as f:
            return f.read().split()
    except FileNotFoundError:
        return []


# ---------- protocol (in-thread) ----------

def test_request_response_and_model_loaded_once(served):
    d, c = served
    for _ in range(3):
        r = c.predict("ml", "app: x\nscreen: y", Q)
        assert r["answers"]["op"] == {"choice": "CLICK", "confidence": 0.9}
    assert d.loads == ["ml"]  # loaded once, not per request
    st = c.status("ml")
    assert st["daemon"] == "running" and st["model_loaded"] and st["version"] == dm.__version__ and st["models_loaded"] == ["ml"]
    assert st["requests"] >= 4 and "warmup_ms" in st["models"]["ml"] and "load_s" in st["models"]["ml"]


def test_models_load_lazily_and_separately(served):
    d, c = served
    c.predict("ml", "s", Q)
    assert d.loads == ["ml"]
    c.predict("en", "s", Q)
    assert d.loads == ["ml", "en"] and c.status()["models_loaded"] == ["en", "ml"]


def test_request_errors_surface_without_restart(served):
    _, c = served
    with pytest.raises(DaemonError, match="unknown model"):
        c.predict("xx", "s", Q)
    assert c.restarts == 0


def test_runtime_files_are_private_and_outside_the_repo(served, paths):
    d, _ = served
    assert oct(os.stat(paths.sock).st_mode & 0o777) == "0o600"
    assert read_pid(paths) == os.getpid()  # in-thread: this process
    repo = os.path.dirname(os.path.dirname(HERE))
    assert not paths.sock.startswith(repo) and not paths.root.startswith(repo)


def test_long_runtime_dir_gets_a_short_socket(tmp_path):
    p = Paths(str(tmp_path / ("x" * 120)))
    assert len(p.sock) <= dm.SOCK_PATH_MAX and p.sock.endswith(".sock")
    assert Paths(str(tmp_path / ("x" * 120))).sock == p.sock  # stable per runtime dir


def test_duplicate_daemon_is_refused(served, paths):
    other = LayaDaemon(paths, fake_laya.load)
    assert other.acquire() is False
    with pytest.raises(DaemonError, match="already running"):
        other.serve(install_signals=False)
    assert lock_held(paths)


def test_concurrent_requests_are_serialized(served, monkeypatch):
    _, c = served
    monkeypatch.setenv("FAKE_LAYA_DELAY", "0.05")
    fake_laya.FakeLaya.max_active = 0
    errors = []

    def go():
        try:
            DaemonClient(c.paths).predict("ml", "s", Q)
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)
    ts = [threading.Thread(target=go) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert errors == [] and fake_laya.FakeLaya.max_active == 1


def test_shutdown_releases_socket_pid_and_lock(paths):
    d = LayaDaemon(paths, fake_laya.load, idle_exit=0)
    t = threading.Thread(target=d.serve, kwargs={"install_signals": False}, daemon=True)
    t.start()
    c = DaemonClient(paths)
    deadline = time.time() + 5
    while not c.ping() and time.time() < deadline:
        time.sleep(0.01)
    assert os.path.exists(paths.sock) and lock_held(paths)
    c.request({"op": "shutdown"})
    t.join(5)
    assert not os.path.exists(paths.sock) and not os.path.exists(paths.pid) and not lock_held(paths)


def test_idle_daemon_exits(paths):
    d = LayaDaemon(paths, fake_laya.load, idle_exit=0.01)
    t0 = time.time()
    d.serve(install_signals=False)  # returns by itself
    assert time.time() - t0 < 5 and not os.path.exists(paths.sock)


def test_decision_client_is_one_request_per_step(served):
    from helpers import snap
    _, c = served
    client = DaemonDecisionClient("ml", c)
    decide(client, "open Bluetooth", snap("settings"), [], "done?")  # first: ping + load + predict
    before = c.status()
    d = decide(client, "open Bluetooth", snap("settings"), [], "done?")
    after = c.status()
    assert d["operation"] in ("CLICK", "BACK", "SCROLL_DOWN", "SCROLL_UP", "DONE") and "p_done" in d
    # operation, target and done in one forward pass, one round trip
    assert after["predicts"] - before["predicts"] == 1
    assert after["requests"] - before["requests"] == 2  # the predict + the status call itself


# ---------- lifecycle (spawned process) ----------

def test_predict_starts_the_daemon_once(spawned, paths, tmp_path):
    c = spawned()
    assert c.status()["daemon"] == "stopped"
    assert c.predict("ml", "s", Q)["answers"]["done"] == {"noul": 0.25}
    assert c.started_daemon and lock_held(paths)
    pid = read_pid(paths)
    c2 = spawned()
    c2.predict("ml", "s", Q)
    assert not c2.started_daemon and read_pid(paths) == pid  # found it running: no second daemon
    assert loads(tmp_path) == ["ml"]  # and the model stayed loaded across clients


def test_stale_pid_and_socket_are_recovered(spawned, paths):
    paths.ensure()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    with open(paths.pid, "w") as f:
        f.write(str(dead.pid))
    with open(paths.sock, "w") as f:
        f.write("not a socket")
    assert clean_stale(paths) and not os.path.exists(paths.pid)
    with open(paths.sock, "w") as f:  # stale again; the client cleans it up itself
        f.write("x")
    c = spawned()
    c.predict("ml", "s", Q)
    assert c.started_daemon and read_pid(paths) != dead.pid


def test_crashed_daemon_is_restarted_once(spawned, paths, tmp_path):
    c = spawned()
    c.predict("ml", "s", Q)
    os.kill(read_pid(paths), signal.SIGKILL)
    time.sleep(0.2)
    assert c.predict("ml", "s", Q)["answers"]
    assert c.restarts == 1 and loads(tmp_path) == ["ml", "ml"]


def test_hung_daemon_times_out_restarts_then_errors(spawned, paths):
    c = spawned(env={"FAKE_LAYA_DELAY": "5"}, timeout=0.5)
    c.ensure_running()
    c._call({"op": "load", "model": "ml"}, 30)  # warm-up predict sleeps too
    c.loaded.add("ml")
    with pytest.raises(DaemonError, match="unavailable after one restart"):
        c.predict("ml", "s", Q)
    assert c.restarts == 1  # restarted once, retried once, then gave up


def test_daemon_that_never_starts_is_a_clear_error(paths, tmp_path):
    c = DaemonClient(paths, spawn_cmd=[sys.executable, "-c", "print('broken install')"], start_timeout=1)
    with pytest.raises(DaemonError, match="did not become ready within 1s") as e:
        c.predict("ml", "s", Q)
    assert "broken install" in str(e.value)  # the log tail is in the message


def test_sigterm_cleans_up(spawned, paths):
    c = spawned()
    c.ensure_running()
    pid = read_pid(paths)
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 5
    while lock_held(paths) and time.time() < deadline:
        time.sleep(0.05)
    assert not lock_held(paths) and not os.path.exists(paths.sock) and not os.path.exists(paths.pid)


def test_stop_command(spawned, paths):
    c = spawned()
    c.ensure_running()
    assert c.stop() is True
    assert c.status()["daemon"] == "stopped" and not lock_held(paths)
    assert c.stop() is False
