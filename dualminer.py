#!/usr/bin/env python3
"""dualminer - one supervisor that mines Monero (xmrig) and Verus (ccminer),
each pinned to its own CPU cores, with a live toggle between
monero-only / verus-only / both.

    ./dualminer.py                 run in the foreground (keys: b m v p s q)
    ./dualminer.py run --mode both
    ./dualminer.py mode monero     switch a running instance
    ./dualminer.py status
"""
import argparse
import ctypes
import glob
import json
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
MODES = ("both", "monero", "verus", "off")
ALIASES = {"xmr": "monero", "vrsc": "verus", "dual": "both", "pause": "off"}
KEYS = {"b": "mode both", "m": "mode monero", "v": "mode verus", "p": "mode off",
        "s": "status", "q": "quit"}
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

try:
    _libc = ctypes.CDLL(None, use_errno=True)
except OSError:
    _libc = None
PR_SET_PDEATHSIG = 1

_print_lock = threading.Lock()


def log(msg, tag="dual"):
    with _print_lock:
        print(f"{time.strftime('%H:%M:%S')} [{tag:5}] {msg}", flush=True)


# ---------------------------------------------------------------- CPU cores

def parse_cpu_list(text):
    """'0-3,6' -> [0, 1, 2, 3, 6]"""
    cpus = set()
    for part in str(text).split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            cpus.update(range(int(lo), int(hi) + 1))
        elif part:
            cpus.add(int(part))
    return sorted(cpus)


def detect_clusters():
    """Return (big, little) core lists. On an Orange Pi 5 (RK3588S) this is
    the Cortex-A76 cores 4-7 and the Cortex-A55 cores 0-3."""
    cpus = sorted(os.sched_getaffinity(0))

    def read(cpu, name):
        try:
            with open(f"/sys/devices/system/cpu/cpu{cpu}/{name}") as f:
                return int(f.read())
        except (OSError, ValueError):
            return None

    for name in ("cpu_capacity", "cpufreq/cpuinfo_max_freq"):
        vals = {c: read(c, name) for c in cpus}
        if None not in vals.values() and len(set(vals.values())) > 1:
            low = min(vals.values())
            return ([c for c in cpus if vals[c] != low],
                    [c for c in cpus if vals[c] == low])
    half = len(cpus) // 2  # symmetric CPU: just split it
    return cpus[half:], cpus[:half]


def resolve_cores(spec, where):
    big, little = detect_clusters()
    online = sorted(os.sched_getaffinity(0))
    if isinstance(spec, list):
        cores = sorted(set(int(c) for c in spec))
    elif spec == "all":
        cores = online
    elif spec == "big":
        cores = big
    elif spec == "little":
        cores = little
    else:
        cores = parse_cpu_list(spec)
    if not cores:
        sys.exit(f"config: {where} resolves to no CPU cores")
    missing = [c for c in cores if c not in online]
    if missing:
        sys.exit(f"config: {where} names cores {missing} that this machine does not have")
    return tuple(cores)


# ------------------------------------------------------------------- miners

class Miner:
    key = tag = ""

    def __init__(self, cfg, cfg_dir, run_dir):
        self.cfg = cfg
        self.cfg_dir = cfg_dir
        self.run_dir = run_dir
        self.proc = None
        self.cores = None
        self.started = 0.0
        self.delay = 0
        self.next_start = 0.0
        self.solo = resolve_cores(cfg.get("cores_solo", "all"), f"{self.key}.cores_solo")
        self.dual = resolve_cores(cfg["cores_dual"], f"{self.key}.cores_dual")

    def binary(self):
        path = self.cfg["binary"]
        if "/" in path:
            return os.path.join(self.cfg_dir, os.path.expanduser(path))
        return shutil.which(path) or path

    def problems(self):
        out = []
        wallet = self.cfg.get("wallet", "")
        if not wallet or wallet.startswith("YOUR_"):
            out.append(f"{self.key}.wallet is not set in the config")
        if not os.access(self.binary(), os.X_OK):
            out.append(f"{self.key} miner binary not found: {self.binary()}")
        return out

    def command(self, cores):
        raise NotImplementedError

    def hashrate(self):
        raise NotImplementedError

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, cores):
        def preexec():
            os.sched_setaffinity(0, cores)
            if _libc:  # die with the supervisor, even if it is SIGKILLed
                _libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM)

        try:
            self.proc = subprocess.Popen(
                self.command(cores), cwd=self.run_dir, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                errors="replace", bufsize=1, start_new_session=True, preexec_fn=preexec)
        except OSError as e:
            self.proc = None
            self.delay = 60
            self.next_start = time.time() + self.delay
            log(f"cannot start {self.key}: {e}; retry in {self.delay}s")
            return
        self.cores = cores
        self.started = time.time()
        log(f"started {self.key} (pid {self.proc.pid}) on cores {fmt_cores(cores)}")
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc):
        for line in proc.stdout:
            line = ANSI.sub("", line).rstrip()
            if line:
                log(line, self.tag)

    def stop(self):
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is None:
            log(f"stopping {self.key}")
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            except ProcessLookupError:
                pass
        self.proc = None
        self.cores = None

    def enforce_affinity(self):
        """Pull back any miner thread that pinned itself outside our cores."""
        allowed = set(self.cores)
        try:
            tids = os.listdir(f"/proc/{self.proc.pid}/task")
        except OSError:
            return
        for tid in tids:
            try:
                if not os.sched_getaffinity(int(tid)) <= allowed:
                    os.sched_setaffinity(int(tid), allowed)
            except OSError:
                pass


class Monero(Miner):
    key, tag = "monero", "XMR"

    def command(self, cores):
        c = self.cfg
        conf = {
            "autosave": False,
            "http": {"enabled": True, "host": "127.0.0.1", "port": c.get("api_port", 18088),
                     "restricted": True},
            "opencl": {"enabled": False},
            "cuda": {"enabled": False},
            # one thread per listed core, each pinned to that core
            "cpu": {"enabled": True, "huge-pages": True, "rx": list(cores)},
            "pools": [{
                "coin": "monero", "url": c["pool"], "user": c["wallet"],
                "pass": c.get("password", "x"), "rig-id": c.get("worker", ""),
                "tls": bool(c.get("tls", False)), "keepalive": True,
            }],
        }
        path = os.path.join(self.run_dir, "xmrig.json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(conf, f, indent=2)
        return [self.binary(), "-c", path] + list(c.get("extra_args", []))

    def hashrate(self):
        url = f"http://127.0.0.1:{self.cfg.get('api_port', 18088)}/2/summary"
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                hs = json.load(r)["hashrate"]["total"][0]
        except (OSError, ValueError, KeyError, IndexError):
            return "n/a"
        return "warming up" if hs is None else f"{hs:.0f} H/s"


class Verus(Miner):
    key, tag = "verus", "VRSC"

    def command(self, cores):
        c = self.cfg
        mask = sum(1 << cpu for cpu in cores)
        user = c["wallet"] + (f".{c['worker']}" if c.get("worker") else "")
        return [self.binary(), "-a", "verus", "-o", c["pool"], "-u", user,
                "-p", c.get("password", "x"), "-t", str(len(cores)),
                "--cpu-affinity", hex(mask),
                "-b", f"127.0.0.1:{c.get('api_port', 4068)}"] + list(c.get("extra_args", []))

    def hashrate(self):
        try:
            with socket.create_connection(("127.0.0.1", self.cfg.get("api_port", 4068)), 2) as s:
                s.sendall(b"summary")
                data = b""
                while chunk := s.recv(4096):
                    data += chunk
            fields = dict(kv.split("=", 1) for kv in data.decode(errors="replace").split(";")
                          if "=" in kv)
            khs = float(fields["KHS"])
        except (OSError, ValueError, KeyError):
            return "n/a"
        return f"{khs / 1000:.2f} MH/s" if khs >= 1000 else f"{khs:.1f} kH/s"


def fmt_cores(cores):
    return ",".join(map(str, cores))


# --------------------------------------------------------------- supervisor

class Supervisor:
    def __init__(self, cfg_path, mode_override):
        with open(cfg_path) as f:
            cfg = json.load(f)
        cfg_dir = os.path.dirname(os.path.abspath(cfg_path))
        self.run_dir = os.path.join(cfg_dir, "run")
        os.makedirs(self.run_dir, exist_ok=True)
        self.mode_file = os.path.join(self.run_dir, "mode")
        self.miners = [Monero(cfg["monero"], cfg_dir, self.run_dir),
                       Verus(cfg["verus"], cfg_dir, self.run_dir)]
        self.q = queue.SimpleQueue()
        self.mode = "off"

        mode = mode_override or self._saved_mode() or cfg.get("mode", "both")
        mode = ALIASES.get(mode, mode)
        if mode not in MODES:
            sys.exit(f"unknown mode '{mode}' (choose from {', '.join(MODES)})")
        problems = self._problems(mode)
        if problems:
            sys.exit("cannot start in mode '%s':\n  %s" % (mode, "\n  ".join(problems)))
        self.mode = mode

        overlap = set(self.miners[0].dual) & set(self.miners[1].dual)
        if overlap:
            log(f"warning: cores {fmt_cores(sorted(overlap))} are given to both coins in dual mode")

    def _saved_mode(self):
        try:
            with open(self.mode_file) as f:
                return f.read().strip() or None
        except OSError:
            return None

    def _problems(self, mode):
        return [p for m in self.miners if self._want(m, mode) for p in m.problems()]

    @staticmethod
    def _want(miner, mode):
        if mode == "both":
            return miner.dual
        return miner.solo if mode == miner.key else None

    def set_mode(self, mode):
        mode = ALIASES.get(mode, mode)
        if mode not in MODES:
            return f"unknown mode '{mode}' (choose from {', '.join(MODES)})"
        problems = self._problems(mode)
        if problems:
            return "refusing mode '%s': %s" % (mode, "; ".join(problems))
        self.mode = mode
        with open(self.mode_file, "w") as f:
            f.write(mode + "\n")
        return f"mode -> {mode}"

    def status(self):
        lines = [f"mode: {self.mode}"]
        for m in self.miners:
            if m.running():
                up = int(time.time() - m.started)
                lines.append(f"{m.key:7} running  cores {fmt_cores(m.cores):12} "
                             f"up {up // 3600}h{up % 3600 // 60:02}m  {m.hashrate()}")
            else:
                lines.append(f"{m.key:7} stopped")
        temps = []
        for path in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
            try:
                with open(path) as f:
                    temps.append(int(f.read()) / 1000)
            except (OSError, ValueError):
                pass
        if temps:
            lines.append(f"temp: {max(temps):.0f} C")
        return "\n".join(lines)

    def tick(self):
        now = time.time()
        for m in self.miners:
            if m.proc is not None and m.proc.poll() is not None:
                ran = now - m.started
                m.delay = 5 if ran > 60 else min(max(m.delay * 2, 5), 120)
                log(f"{m.key} exited (code {m.proc.returncode}) after {ran:.0f}s; "
                    f"restart in {m.delay}s")
                m.proc = m.cores = None
                m.next_start = now + m.delay
            want = self._want(m, self.mode)
            if want is None:
                m.stop()
                m.delay, m.next_start = 0, 0.0
                continue
            if m.proc is not None and m.cores != want:
                m.stop()  # core set changed: restart on the new cores
            if m.proc is not None:
                m.enforce_affinity()
            elif now >= m.next_start:
                m.start(want)

    def handle(self, cmd):
        parts = cmd.split()
        if not parts:
            return "empty command"
        if parts[0] == "mode" and len(parts) == 2:
            return self.set_mode(parts[1])
        if parts[0] == "status":
            return self.status()
        if parts[0] == "quit":
            return None
        return f"unknown command '{cmd}'"

    # ---- inputs: control socket, keyboard, signals

    def _open_socket(self):
        # relative path: AF_UNIX paths are limited to ~107 bytes
        os.chdir(self.run_dir)
        path = "dualminer.sock"
        if os.path.exists(path):
            try:
                with socket.socket(socket.AF_UNIX) as probe:
                    probe.connect(path)
                sys.exit("dualminer is already running (use './dualminer.py mode ...' to switch)")
            except OSError:
                os.unlink(path)
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(path)
        os.chmod(path, 0o600)
        sock.listen(4)
        return sock, path

    def _control(self, sock):
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.settimeout(5)
                    line = conn.makefile().readline().strip()
                    reply = queue.SimpleQueue()
                    self.q.put((line, reply))
                    conn.settimeout(40)
                    conn.sendall((reply.get(timeout=40) + "\n").encode())
                except (OSError, queue.Empty):
                    pass

    def _keyboard(self, fd):
        while True:
            try:
                ch = os.read(fd, 1).decode(errors="ignore").lower()
            except OSError:
                return
            if not ch:
                return
            if ch in KEYS:
                self.q.put((KEYS[ch], None))
            elif ch in ("h", "?"):
                log("keys: b=both  m=monero only  v=verus only  p=pause  s=status  q=quit")

    def run(self):
        sock, sock_path = self._open_socket()
        threading.Thread(target=self._control, args=(sock,), daemon=True).start()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: self.q.put(("quit", None)))

        saved_tty = None
        if sys.stdin.isatty():
            import termios
            import tty
            fd = sys.stdin.fileno()
            saved_tty = (termios, fd, termios.tcgetattr(fd))
            tty.setcbreak(fd)
            threading.Thread(target=self._keyboard, args=(fd,), daemon=True).start()
            log("keys: b=both  m=monero only  v=verus only  p=pause  s=status  q=quit")

        log(f"mode: {self.mode}")
        try:
            while True:
                self.tick()
                try:
                    cmd, reply = self.q.get(timeout=1)
                except queue.Empty:
                    continue
                result = self.handle(cmd)
                if result is None:
                    if reply:
                        reply.put("shutting down")
                    break
                if reply:
                    reply.put(result)
                else:
                    for line in result.splitlines():
                        log(line)
        finally:
            for m in self.miners:
                m.stop()
            sock.close()
            try:
                os.unlink(sock_path)
            except OSError:
                pass
            if saved_tty:
                termios, fd, attrs = saved_tty
                termios.tcsetattr(fd, termios.TCSADRAIN, attrs)
            log("stopped")


def send(cfg_path, cmd):
    try:
        os.chdir(os.path.join(os.path.dirname(os.path.abspath(cfg_path)), "run"))
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(45)
            s.connect("dualminer.sock")
            s.sendall((cmd + "\n").encode())
            print(s.makefile().read().rstrip())
    except OSError as e:
        sys.exit(f"dualminer is not running ({e})")


def main():
    ap = argparse.ArgumentParser(description="Monero + Verus dual CPU miner supervisor")
    ap.add_argument("-c", "--config", default=os.path.join(ROOT, "config.json"))
    sub = ap.add_subparsers(dest="cmd")
    run = sub.add_parser("run", help="run the miner (default)")
    run.add_argument("--mode", help="both | monero | verus | off (default: last used)")
    mode = sub.add_parser("mode", help="switch a running instance")
    mode.add_argument("mode", help="both | monero | verus | off")
    sub.add_parser("status", help="show what a running instance is doing")
    sub.add_parser("quit", help="stop a running instance")
    sub.add_parser("cores", help="show detected big/little cores")
    args = ap.parse_args()

    if args.cmd == "cores":
        big, little = detect_clusters()
        print(f"big:    {fmt_cores(big)}\nlittle: {fmt_cores(little)}")
    elif args.cmd == "mode":
        send(args.config, f"mode {args.mode}")
    elif args.cmd in ("status", "quit"):
        send(args.config, args.cmd)
    else:
        if not os.path.exists(args.config):
            sys.exit(f"{args.config} not found - copy config.example.json to config.json "
                     "and fill in your wallets")
        Supervisor(args.config, getattr(args, "mode", None)).run()


if __name__ == "__main__":
    main()
