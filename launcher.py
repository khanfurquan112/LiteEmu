"""
LiteEmu Launcher - QEMU frontend for 4 GB RAM / HDD laptops.
Python 3.9+, standard library only (Tkinter), so the .exe stays small.
"""
import ctypes
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

IS_WIN = os.name == "nt"
NO_WINDOW = 0x08000000 if IS_WIN else 0          # CREATE_NO_WINDOW
APP_DIR = os.path.dirname(sys.executable if getattr(sys, "frozen", False)
                          else os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
SNAPSHOT = "liteemu"
LOW_RAM_WARN_MB = 400
INT_KEYS = {"ram_mb", "cpus", "disk_gb", "adb_port"}
QEMU_DIRS = [r"C:\Program Files\qemu", r"C:\Program Files (x86)\qemu"]


# ---------- host helpers ----------
class _MemStatus(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def host_memory_mb():
    """Return (total_mb, free_mb). Windows only; (0, 0) elsewhere."""
    if not IS_WIN:
        return 0, 0
    s = _MemStatus()
    s.dwLength = ctypes.sizeof(s)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
    return s.ullTotalPhys // 2**20, s.ullAvailPhys // 2**20


def set_above_normal_priority(pid):
    """Game Boost: give the emulator process more CPU priority than background apps."""
    if not IS_WIN:
        return
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(0x0200, False, pid)      # PROCESS_SET_INFORMATION
    if handle:
        k32.SetPriorityClass(handle, 0x00008000)      # ABOVE_NORMAL_PRIORITY_CLASS
        k32.CloseHandle(handle)


def find_tool(name):
    found = shutil.which(name)
    if found:
        return found
    for folder in QEMU_DIRS:
        path = os.path.join(folder, name + ".exe")
        if os.path.isfile(path):
            return path
    return ""


# ---------- config ----------
def default_config():
    total, _ = host_memory_mb()
    return {
        "qemu": find_tool("qemu-system-x86_64"),
        "qemu_img": find_tool("qemu-img"),
        "iso": "",
        "disk": os.path.join(APP_DIR, "data.qcow2"),
        "disk_gb": 8,
        "prealloc": "full",
        "ram_mb": 1024 if total <= 4600 else 2048,
        "cpus": 2,
        "accel": "whpx",
        "audio": True,
        "adb_port": 5555,
        "monitor_port": 4444,
        "boost": True,
    }


def load_config():
    cfg = default_config()
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


# ---------- QEMU ----------
def build_qemu_cmd(cfg, install=False, load_snapshot=False):
    cmd = [cfg["qemu"], "-name", "LiteEmu",
           "-accel", cfg["accel"], "-cpu", "qemu64",
           "-smp", str(cfg["cpus"]), "-m", str(cfg["ram_mb"]),
           "-drive", f"file={cfg['disk']},format=qcow2,if=virtio,cache=writeback",
           "-vga", "std", "-usb", "-device", "usb-tablet",
           "-netdev", f"user,id=n0,hostfwd=tcp::{cfg['adb_port']}-:5555",
           "-device", "virtio-net-pci,netdev=n0",
           "-monitor", f"tcp:127.0.0.1:{cfg['monitor_port']},server,nowait"]
    cmd += ["-cdrom", cfg["iso"], "-boot", "d"] if install else ["-boot", "c"]
    if cfg["audio"]:
        cmd += ["-audiodev", "dsound,id=snd0", "-device", "AC97,audiodev=snd0"]
    if load_snapshot:
        cmd += ["-loadvm", SNAPSHOT]
    return cmd


def _read_until_prompt(sock):
    buf = b""
    while b"(qemu)" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf.decode(errors="replace")


def monitor_cmd(port, command, timeout=180):
    """Send one command to the QEMU monitor and return its cleaned-up output."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.settimeout(timeout)
        _read_until_prompt(sock)                       # banner
        sock.sendall(command.encode() + b"\r\n")
        text = _read_until_prompt(sock)
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text).replace("(qemu)", "")
    lines = [ln.strip() for ln in text.replace("\r", "").split("\n")]
    return "\n".join(ln for ln in lines if ln and ln != command)


# ---------- GUI ----------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LiteEmu Launcher")
        self.resizable(False, False)
        self.cfg = load_config()
        self.proc = None
        self.paused = False
        self.low_ram_warned = False
        self.vars = {}
        self.msgs = queue.Queue()
        self.status = tk.StringVar()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(500, self.tick)

    def _build_ui(self):
        pad = {"padx": 8, "pady": 3}

        files = ttk.LabelFrame(self, text="Files")
        files.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        rows = [("QEMU program", "qemu", [("Program", "*.exe")]),
                ("qemu-img program", "qemu_img", [("Program", "*.exe")]),
                ("Android ISO", "iso", [("ISO image", "*.iso")]),
                ("Disk image", "disk", [("Disk image", "*.qcow2")])]
        for i, (label, key, types) in enumerate(rows):
            self.vars[key] = tk.StringVar(value=self.cfg[key])
            ttk.Label(files, text=label).grid(row=i, column=0, sticky="w", **pad)
            ttk.Entry(files, textvariable=self.vars[key], width=52).grid(row=i, column=1, **pad)
            ttk.Button(files, text="Browse", command=lambda k=key, t=types: self.browse(k, t)
                       ).grid(row=i, column=2, **pad)

        opts = ttk.LabelFrame(self, text="Settings")
        opts.grid(row=1, column=0, sticky="ew", padx=8, pady=4)
        combos = [("Guest RAM (MB)", "ram_mb", [768, 1024, 1536, 2048]),
                  ("CPU cores", "cpus", [1, 2, 3, 4]),
                  ("Disk size (GB)", "disk_gb", [4, 6, 8, 12, 16]),
                  ("Pre-allocate disk", "prealloc", ["full", "metadata", "off"]),
                  ("Acceleration", "accel", ["whpx", "whpx,kernel-irqchip=off", "tcg"]),
                  ("ADB port", "adb_port", [5555, 5556, 5557])]
        for i, (label, key, values) in enumerate(combos):
            self.vars[key] = tk.StringVar(value=str(self.cfg[key]))
            r, c = divmod(i, 2)
            ttk.Label(opts, text=label).grid(row=r, column=c * 2, sticky="w", **pad)
            ttk.Combobox(opts, textvariable=self.vars[key], values=values, width=22
                         ).grid(row=r, column=c * 2 + 1, **pad)
        self.vars["audio"] = tk.BooleanVar(value=self.cfg["audio"])
        self.vars["boost"] = tk.BooleanVar(value=self.cfg["boost"])
        ttk.Checkbutton(opts, text="Sound", variable=self.vars["audio"]
                        ).grid(row=3, column=0, sticky="w", **pad)
        ttk.Checkbutton(opts, text="Game Boost (higher CPU priority)", variable=self.vars["boost"]
                        ).grid(row=3, column=1, columnspan=3, sticky="w", **pad)

        bar = ttk.Frame(self)
        bar.grid(row=2, column=0, sticky="ew", padx=8, pady=4)
        buttons = [("Create disk", self.create_disk), ("Install Android", self.install),
                   ("Start", self.start), ("Start from snapshot", self.start_snapshot),
                   ("Pause / resume", self.toggle_pause), ("Save snapshot", self.save_snapshot),
                   ("Shut down", self.shutdown), ("Force quit", self.force_quit)]
        for i, (text, fn) in enumerate(buttons):
            ttk.Button(bar, text=text, command=fn).grid(row=i // 4, column=i % 4,
                                                        sticky="ew", padx=3, pady=3)
            bar.columnconfigure(i % 4, weight=1)

        self.out = tk.Text(self, height=9, width=84, state="disabled", wrap="word")
        self.out.grid(row=3, column=0, padx=8, pady=4)
        ttk.Label(self, textvariable=self.status).grid(row=4, column=0, sticky="w", padx=8, pady=(0, 8))

    # -- small helpers --
    def browse(self, key, types):
        path = filedialog.askopenfilename(filetypes=types + [("All files", "*.*")])
        if path:
            self.vars[key].set(os.path.normpath(path))

    def log(self, text):
        self.msgs.put(str(text).rstrip())

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def collect(self):
        for key, var in self.vars.items():
            value = var.get()
            if key in INT_KEYS:
                try:
                    value = int(value)
                except ValueError:
                    raise ValueError(f"'{key}' must be a number.")
            self.cfg[key] = value
        save_config(self.cfg)

    def background(self, fn):
        threading.Thread(target=fn, daemon=True).start()

    def monitor(self, command, timeout=180):
        def job():
            try:
                out = monitor_cmd(self.cfg["monitor_port"], command, timeout)
                self.log(f"[{command}] {out or 'done'}")
            except OSError as err:
                self.log(f"[{command}] could not reach the emulator monitor: {err}")
        self.background(job)

    # -- actions --
    def create_disk(self):
        try:
            self.collect()
        except ValueError as err:
            return messagebox.showerror("Check settings", str(err))
        c = self.cfg
        if not os.path.isfile(c["qemu_img"]):
            return messagebox.showerror("qemu-img missing", "Select qemu-img.exe in the Files section.")
        if os.path.exists(c["disk"]):
            if not messagebox.askyesno("Replace disk?", "A disk image already exists. Delete it and "
                                       "create a new one? All Android data on it will be lost."):
                return
            os.remove(c["disk"])

        def job():
            self.log(f"Creating {c['disk_gb']} GB disk ({c['prealloc']} pre-allocation). "
                     "Full pre-allocation can take a few minutes on an HDD...")
            r = subprocess.run([c["qemu_img"], "create", "-f", "qcow2", "-o",
                                f"preallocation={c['prealloc']}", c["disk"], f"{c['disk_gb']}G"],
                               stdin=subprocess.DEVNULL, capture_output=True, text=True,
                               creationflags=NO_WINDOW)
            self.log((r.stdout + r.stderr).strip() or "Disk ready.")
        self.background(job)

    def launch(self, install=False, snapshot=False):
        if self.running():
            return messagebox.showinfo("Already running", "The emulator is already running.")
        try:
            self.collect()
        except ValueError as err:
            return messagebox.showerror("Check settings", str(err))
        c = self.cfg
        if not os.path.isfile(c["qemu"]):
            return messagebox.showerror("QEMU missing", "Select qemu-system-x86_64.exe in the Files section.")
        if not os.path.isfile(c["disk"]):
            return messagebox.showerror("No disk", "Click 'Create disk' first.")
        if install and not os.path.isfile(c["iso"]):
            return messagebox.showerror("No ISO", "Select an Android-x86 or Bliss OS .iso file first.")
        _, free = host_memory_mb()
        if free and free < c["ram_mb"] + 300:
            if not messagebox.askyesno("Low memory", f"Only {free} MB of RAM is free. The emulator may "
                                       "slow your PC. Close other apps, or start anyway?"):
                return
        cmd = build_qemu_cmd(c, install, snapshot)
        self.log("> " + " ".join(cmd))
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, text=True, creationflags=NO_WINDOW)
        self.paused = False
        self.low_ram_warned = False
        threading.Thread(target=self.pump_errors, args=(self.proc,), daemon=True).start()
        if c["boost"]:
            set_above_normal_priority(self.proc.pid)
            self.log("Game Boost on: emulator runs at above-normal priority.")

    def pump_errors(self, proc):
        for line in proc.stderr:
            self.log(line)
        self.log(f"Emulator closed (exit code {proc.wait()}).")

    def install(self):
        self.launch(install=True)

    def start(self):
        self.launch()

    def start_snapshot(self):
        self.launch(snapshot=True)

    def toggle_pause(self):
        if not self.running():
            return
        self.monitor("cont" if self.paused else "stop")
        self.paused = not self.paused

    def save_snapshot(self):
        if self.running():
            self.log("Saving snapshot. This can take a minute on an HDD. "
                     "If QEMU reports an error, your QEMU build does not support snapshots with WHPX.")
            self.monitor(f"savevm {SNAPSHOT}")

    def shutdown(self):
        if self.running():
            self.monitor("system_powerdown")

    def force_quit(self):
        if self.running() and messagebox.askyesno(
                "Force quit?", "This is like pulling the power cable. Unsaved Android data can be lost."):
            self.proc.terminate()

    def on_close(self):
        if self.running():
            if not messagebox.askyesno("Emulator is running", "Force quit the emulator and close?"):
                return
            self.proc.terminate()
        try:
            self.collect()
        except ValueError:
            pass
        self.destroy()

    def tick(self):
        while not self.msgs.empty():
            self.out.configure(state="normal")
            self.out.insert("end", self.msgs.get() + "\n")
            self.out.see("end")
            self.out.configure(state="disabled")
        total, free = host_memory_mb()
        state = "paused" if self.running() and self.paused else "running" if self.running() else "stopped"
        ram = f"   |   Host RAM free: {free} MB of {total} MB" if total else ""
        self.status.set(f"Emulator: {state}{ram}")
        if self.running() and free and free < LOW_RAM_WARN_MB and not self.low_ram_warned:
            self.low_ram_warned = True
            self.log(f"Warning: only {free} MB of host RAM is free. Close other apps to avoid lag.")
        self.after(1000, self.tick)


if __name__ == "__main__":
    App().mainloop()
