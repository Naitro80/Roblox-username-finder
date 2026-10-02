import itertools
import json
import os
import queue
import re
import shutil
import string
import sys
import time
import requests
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from itertools import islice
from threading import Event, Lock, Thread
from colorama import Back, Fore, Style, init

try:
    import msvcrt
except ImportError:
    msvcrt = None

LOGO = r"""
███╗░░██╗██╗░░██╗
████╗░██║██║░░██║
██╔██╗██║███████║
██║╚████║██╔══██║
██║░╚███║██║░░██║
╚═╝░░╚══╝╚═╝░░╚═╝
"""

RESET = Style.RESET_ALL
BOLD = Style.BRIGHT
GRAY = Fore.LIGHTBLACK_EX
CYAN = Fore.CYAN
GREEN = Fore.GREEN
YELLOW = Fore.YELLOW
RED = Fore.RED

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
WORDS_PATH = os.path.join(BASE_DIR, "words.txt")
LISTS_DIR = os.path.join(BASE_DIR, "lists")

WEBHOOK_RE = re.compile(
    r"^https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/[\w-]+$"
)
ROBLOX_NAME_RE = re.compile(r"^(?!_)(?!.*_.*_)(?!.*_$)[a-z0-9_]{3,20}$")
MC_NAME_RE = re.compile(r"^[a-z0-9_]{3,16}$")

LETTERS = string.ascii_lowercase
ALNUM = LETTERS + string.digits
CONSONANTS = "bcdfghjklmnpqrstvwxyz"
VOWELS = "aeiou"

CHUNK_SIZE = 200
PASS_PAUSE = 5
MAX_COOLDOWN = 120.0

DEFAULTS = {
    "message_style": "embed",
    "roblox_speed": "fast",
    "minecraft_both": True,
    "show_all": False,
}

ROBLOX_PROFILES = {
    "safe": {"pacer": (0.25, 0.15, 10.0, 5.0), "workers": 4},
    "normal": {"pacer": (0.12, 0.08, 10.0, 5.0), "workers": 6},
    "fast": {"pacer": (0.08, 0.04, 10.0, 5.0), "workers": 8},
}

ROBLOX_CHECK_URL = "https://auth.roblox.com/v1/usernames/validate?Username={}&Birthday=2000-01-01"
MC_BULK_URL_A = "https://api.minecraftservices.com/minecraft/profile/lookup/bulk/byname"
MC_BULK_URL_B = "https://api.mojang.com/profiles/minecraft"
MC_CHECK_URL = "https://api.minecraftservices.com/minecraft/profile/lookup/name/{}"

NETWORK_ERRORS = (
    requests.exceptions.RequestException,
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
)

write_lock = Lock()
print_lock = Lock()
count_lock = Lock()
notify_queue = queue.Queue()
seen = set()
config = {}

resume_event = Event()
resume_event.set()

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
})


class RateLimited(Exception):
    def __init__(self, retry_after=None):
        super().__init__("rate limited")
        self.retry_after = retry_after


class EndpointGone(Exception):
    pass


class Pacer:
    def __init__(self, delay, min_delay, max_delay, cooldown):
        self.delay = delay
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.cooldown = cooldown
        self.next_at = 0.0
        self.paused_until = 0.0
        self.streak = 0
        self.lock = Lock()

    def wait(self):
        resume_event.wait()
        with self.lock:
            now = time.time()
            start = max(now, self.next_at)
            self.next_at = start + self.delay
        pause = start - now
        if pause > 0:
            time.sleep(pause)

    def limited(self, retry_after=None):
        with self.lock:
            now = time.time()
            if now < self.paused_until:
                return self.paused_until - now
            self.streak += 1
            wait = retry_after or min(self.cooldown * 2 ** (self.streak - 1), MAX_COOLDOWN)
            self.delay = min(self.delay * 1.5, self.max_delay)
            self.paused_until = now + wait
            self.next_at = max(self.next_at, self.paused_until)
            return wait

    def ok(self):
        with self.lock:
            self.streak = 0
            self.delay = max(self.delay * 0.97, self.min_delay)

    def pause_left(self):
        return max(0.0, self.paused_until - time.time())


class Stats:
    def __init__(self):
        self.running = False
        self.verbose = False
        self.webhook = False
        self.label = ""
        self.mode_label = ""
        self.filename = ""
        self.checked = 0
        self.found = 0
        self.limited = 0
        self.pass_no = 0
        self.pass_done = 0
        self.pass_total = 0
        self.current = ""
        self.started = time.time()
        self.rate = 0.0
        self.pacers = []


stats = Stats()


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(data):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


def get_setting(key):
    return config.get(key, DEFAULTS[key])


def set_setting(key, value):
    config[key] = value
    save_config(config)


def header_seconds(response):
    for name in ("Retry-After", "x-ratelimit-reset"):
        value = response.headers.get(name)
        if value:
            try:
                return float(value)
            except ValueError:
                pass
    return None


def roblox_check(name):
    r = session.get(ROBLOX_CHECK_URL.format(name), timeout=8)
    if r.status_code == 429:
        raise RateLimited(header_seconds(r))
    code = r.json().get("code")
    return {0: "valid", 1: "taken", 2: "blocked"}.get(code, "invalid")


def minecraft_check(name):
    r = session.get(MC_CHECK_URL.format(name), timeout=8)
    if r.status_code == 429:
        raise RateLimited(header_seconds(r))
    if r.status_code == 200:
        return "taken"
    if r.status_code in (204, 404):
        return "valid"
    return "invalid"


def make_minecraft_bulk(url):
    def bulk(batch):
        r = session.post(url, json=batch, timeout=10)
        if r.status_code == 429:
            raise RateLimited(header_seconds(r))
        if r.status_code in (404, 405, 410):
            raise EndpointGone()
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            raise ValueError("unexpected response")
        return {p["name"].lower() for p in data}

    return bulk


def valid_roblox(name):
    return bool(ROBLOX_NAME_RE.match(name))


def valid_minecraft(name):
    return bool(MC_NAME_RE.match(name))


def build_platforms():
    profile = ROBLOX_PROFILES[get_setting("roblox_speed")]
    delay, min_delay, max_delay, cooldown = profile["pacer"]

    lanes = [
        {
            "name": "services",
            "bulk": make_minecraft_bulk(MC_BULK_URL_A),
            "pacer": Pacer(1.1, 1.0, 15.0, 15.0),
            "dead": False,
        },
        {
            "name": "mojang",
            "bulk": make_minecraft_bulk(MC_BULK_URL_B),
            "pacer": Pacer(1.1, 1.0, 15.0, 15.0),
            "dead": False,
        },
    ]
    if not get_setting("minecraft_both"):
        lanes = lanes[:1]

    return {
        "roblox": {
            "label": "Roblox",
            "charset": ALNUM,
            "check": roblox_check,
            "valid": valid_roblox,
            "lanes": None,
            "batch": 1,
            "workers": profile["workers"],
            "file": os.path.join(BASE_DIR, "available_roblox.txt"),
            "pacer": Pacer(delay, min_delay, max_delay, cooldown),
        },
        "minecraft": {
            "label": "Minecraft",
            "charset": ALNUM + "_",
            "check": minecraft_check,
            "valid": valid_minecraft,
            "lanes": lanes,
            "batch": 10,
            "workers": 8,
            "file": os.path.join(BASE_DIR, "available_minecraft.txt"),
            "pacer": lanes[0]["pacer"],
        },
    }


RARE_SHAPES = {
    3: {"pron": ["CVC", "VCV"], "rep": ["AAA", "AAB", "ABA", "ABB"]},
    4: {"pron": ["CVCV", "VCVC"], "rep": ["AAAA", "AABB", "ABAB", "ABBA", "AAAB", "ABBB"]},
}


def chunked(iterable, n):
    it = iter(iterable)
    while chunk := list(islice(it, n)):
        yield chunk


def pronounceable(shape):
    pools = [CONSONANTS if c == "C" else VOWELS for c in shape]
    return ("".join(p) for p in itertools.product(*pools))


def repeating(shape, chars):
    keys = sorted(set(shape))
    for combo in itertools.permutations(chars, len(keys)):
        mapping = dict(zip(keys, combo))
        yield "".join(mapping[c] for c in shape)


def load_words(length, charset):
    if not os.path.exists(WORDS_PATH):
        return []
    with open(WORDS_PATH, encoding="utf-8") as f:
        words = [w.strip().lower() for w in f]
    return [w for w in words if len(w) == length and all(c in charset for c in w)]


def rare_names(length, charset):
    chars = [c for c in charset if c != "_"]
    names = load_words(length, charset)
    for shape in RARE_SHAPES[length]["rep"]:
        names.extend(repeating(shape, chars))
    for shape in RARE_SHAPES[length]["pron"]:
        names.extend(pronounceable(shape))
    return names


def build_names(platform, mode, lengths, custom=None):
    charset = platform["charset"]
    if mode == "list":
        return list(custom), len(custom)

    if mode == "rare":
        names = []
        for length in lengths:
            names.extend(rare_names(length, charset))
        names = list(dict.fromkeys(names))
        return names, len(names)

    total = sum(len(charset) ** length for length in lengths)

    def generate():
        for length in lengths:
            for combo in itertools.product(charset, repeat=length):
                yield "".join(combo)

    return generate(), total


def load_list(path, platform):
    try:
        with open(path, encoding="utf-8-sig", errors="ignore") as f:
            text = f.read()
    except OSError:
        return [], 0, 0
    raw = [t.lower() for t in re.split(r"[\s,;]+", text) if t]
    unique = list(dict.fromkeys(raw))
    valid = [n for n in unique if platform["valid"](n)]
    return valid, len(unique) - len(valid), len(raw) - len(unique)


def fmt_int(number):
    return f"{number:,}".replace(",", " ")


def fmt_duration(seconds):
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {secs:02d}s"


def render_status():
    if not stats.running:
        return
    width = shutil.get_terminal_size((110, 30)).columns - 1
    pct = min(stats.pass_done / stats.pass_total, 1.0) if stats.pass_total else 0.0
    filled = int(pct * 14)
    bar = "█" * filled + "░" * (14 - filled)
    pause = min((p.pause_left() for p in stats.pacers), default=0.0)
    paused = not resume_event.is_set()

    parts = [(f" {stats.label.upper()} · {stats.mode_label.upper()} ", Back.CYAN + Fore.BLACK)]
    if paused:
        parts.append((" PAUSED ", Back.YELLOW + Fore.BLACK))
    parts.append((f"{bar} {pct * 100:5.1f}%", CYAN))
    if pause >= 1 and not paused:
        parts.append((f"wait {int(pause)}s", YELLOW))
    parts.append((f"hits {stats.found}", GREEN if stats.found else GRAY))
    parts.append((f"{stats.rate:.1f}/s", RESET))
    parts.append((f"{fmt_int(stats.checked)} checked", RESET))
    parts.append((f"pass {stats.pass_no}", RESET))
    if stats.current:
        parts.append((f"now {stats.current}", GRAY))
    if stats.limited:
        parts.append((f"429 x{stats.limited}", YELLOW))
    remaining = stats.pass_total - stats.pass_done
    if stats.rate > 0 and remaining > 0 and not paused:
        parts.append((f"ETA {fmt_duration(remaining / stats.rate)}", GRAY))
    parts.append((f"time {fmt_duration(time.time() - stats.started)}", GRAY))
    if msvcrt:
        parts.append(("P pause · Q quit", GRAY))

    while len(parts) > 2 and sum(len(t) for t, _ in parts) + 3 * (len(parts) - 1) > width:
        parts.pop()

    sep = GRAY + " │ " + RESET
    line = sep.join(color + text + RESET for text, color in parts)
    sys.stdout.write("\r\x1b[2K" + line)


def log(text):
    with print_lock:
        sys.stdout.write("\r\x1b[2K" + text + "\n")
        render_status()
        sys.stdout.flush()


def status_loop():
    samples = deque(maxlen=30)
    while True:
        now = time.time()
        samples.append((now, stats.checked))
        t0, c0 = samples[0]
        stats.rate = (stats.checked - c0) / (now - t0) if now > t0 else 0.0
        with print_lock:
            render_status()
            sys.stdout.flush()
        time.sleep(0.5)


def print_exit_summary(filename):
    sys.stdout.write("\r\x1b[2K")
    print()
    print(GRAY + "  " + "─" * 48 + RESET)
    print(f"  {BOLD}Stopped{RESET}")
    print(f"  {GRAY}Checked{RESET}  {fmt_int(stats.checked)}")
    print(f"  {GRAY}Hits{RESET}     {stats.found}  {GRAY}saved in {os.path.basename(filename)}{RESET}")
    print(f"  {GRAY}Time{RESET}     {fmt_duration(time.time() - stats.started)}")
    print()
    sys.stdout.flush()


def quit_now():
    stats.running = False
    resume_event.set()
    with print_lock:
        print_exit_summary(stats.filename)
    os._exit(0)


def toggle_pause():
    if resume_event.is_set():
        resume_event.clear()
        log(f"{YELLOW}  Paused. Press P to resume.{RESET}")
    else:
        resume_event.set()
        log(f"{GREEN}  Resumed.{RESET}")


def key_listener():
    while True:
        if msvcrt.kbhit():
            key = msvcrt.getwch()
            if key in ("\x00", "\xe0"):
                msvcrt.getwch()
                continue
            key = key.lower()
            if key == "p":
                toggle_pause()
            elif key == "q":
                quit_now()
        time.sleep(0.05)


def build_payload(label, name):
    if get_setting("message_style") == "plain":
        return {"username": "Username Finder", "content": f"Available - {name} ({label})"}

    if label == "Minecraft":
        verify = f"[Check on NameMC](https://namemc.com/search?q={name})"
    else:
        verify = f"[Search on Roblox](https://www.roblox.com/search/users?keyword={name})"

    return {
        "username": "Username Finder",
        "embeds": [{
            "title": "✅ Username available",
            "description": f"**`{name}`**",
            "color": 0x57F287,
            "fields": [
                {"name": "Platform", "value": label, "inline": True},
                {"name": "Length", "value": f"{len(name)} characters", "inline": True},
                {"name": "Found", "value": f"<t:{int(time.time())}:R>", "inline": True},
                {"name": "Verify", "value": verify, "inline": False},
            ],
            "footer": {"text": "Username Finder"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    }


def webhook_worker(url):
    while True:
        label, name = notify_queue.get()
        payload = build_payload(label, name)
        for _ in range(5):
            try:
                r = requests.post(url, json=payload, timeout=8)
                if r.status_code == 429:
                    try:
                        wait = float(r.json().get("retry_after", 1))
                    except (ValueError, TypeError):
                        wait = 1.0
                    time.sleep(wait)
                    continue
                if r.status_code >= 400:
                    log(f"{YELLOW}  webhook error {r.status_code} for {name}{RESET}")
                break
            except requests.exceptions.RequestException as e:
                log(f"{YELLOW}  webhook failed for {name}: {e}{RESET}")
                break
        notify_queue.task_done()
        time.sleep(0.5)


def register_hit(platform, name, output_file):
    with write_lock:
        if name in seen:
            return
        seen.add(name)
        output_file.write(name + "\n")
        output_file.flush()
        stats.found += 1
    if stats.webhook:
        notify_queue.put((platform["label"], name))
    log(f"{Back.GREEN}{Fore.BLACK} AVAILABLE {RESET} {GREEN}{BOLD}{name}{RESET} {GRAY}({platform['label']}){RESET}")


def mark_done(name, checked=True):
    with count_lock:
        if checked:
            stats.checked += 1
        stats.pass_done += 1
        stats.current = name


def log_status(status, name):
    labels = {"taken": (GRAY, "TAKEN"), "blocked": (RED, "BLOCKED"), "invalid": (YELLOW, "INVALID")}
    color, text = labels[status]
    log(f"{color}  {text:<8}{name}{RESET}")


def check_name(platform, name, output_file):
    pacer = platform["pacer"]
    failures = 0
    while True:
        pacer.wait()
        try:
            status = platform["check"](name)
        except RateLimited as e:
            stats.limited += 1
            pacer.limited(e.retry_after)
            continue
        except NETWORK_ERRORS:
            failures += 1
            if failures >= 5:
                mark_done(name, checked=False)
                log(f"{YELLOW}  could not check {name}{RESET}")
                return
            time.sleep(2)
            continue

        pacer.ok()
        mark_done(name)
        if status == "valid":
            register_hit(platform, name, output_file)
        elif stats.verbose:
            log_status(status, name)
        return


def lookup_batch(lane, batch):
    pacer = lane["pacer"]
    failures = 0
    while True:
        pacer.wait()
        try:
            taken = lane["bulk"](batch)
        except RateLimited as e:
            stats.limited += 1
            pacer.limited(e.retry_after)
            continue
        except NETWORK_ERRORS:
            failures += 1
            if failures >= 6:
                log(f"{YELLOW}  skipped {batch[0]} to {batch[-1]} (network error){RESET}")
                return None
            time.sleep(5)
            continue
        pacer.ok()
        return taken


def confirm_task(platform, name, output_file):
    pacer = platform["pacer"]
    for _ in range(10):
        pacer.wait()
        try:
            status = platform["check"](name)
        except RateLimited as e:
            stats.limited += 1
            pacer.limited(e.retry_after)
            continue
        except NETWORK_ERRORS:
            time.sleep(2)
            continue
        pacer.ok()
        if status == "valid":
            register_hit(platform, name, output_file)
        elif stats.verbose:
            log_status(status, name)
        return
    log(f"{YELLOW}  could not confirm {name}{RESET}")


def scan_single(platform, names, output_file, executor):
    for chunk in chunked(names, CHUNK_SIZE):
        list(executor.map(lambda name: check_name(platform, name, output_file), chunk))


def scan_bulk(platform, names, output_file, executor):
    lanes = platform["lanes"]
    batches = chunked(names, platform["batch"])
    batch_lock = Lock()

    def next_batch():
        with batch_lock:
            return next(batches, None)

    def lane_worker(lane):
        while True:
            batch = next_batch()
            if batch is None:
                return
            stats.current = batch[-1]
            while True:
                try:
                    taken = lookup_batch(lane, batch)
                    break
                except EndpointGone:
                    lane["dead"] = True
                    alive = [item for item in lanes if not item["dead"]]
                    if not alive:
                        log(f"{RED}  no working Minecraft endpoint left{RESET}")
                        return
                    log(f"{YELLOW}  {lane['name']} endpoint unavailable, using {alive[0]['name']}{RESET}")
                    lane = alive[0]
            with count_lock:
                stats.pass_done += len(batch)
                if taken is not None:
                    stats.checked += len(batch)
            if taken is None:
                continue
            if stats.verbose:
                lines = [f"{GRAY}  TAKEN   {n}{RESET}" for n in batch if n in taken]
                if lines:
                    log("\n".join(lines))
            for name in batch:
                if name not in taken:
                    executor.submit(confirm_task, platform, name, output_file)

    threads = [Thread(target=lane_worker, args=(lane,), daemon=True) for lane in lanes]
    for thread in threads:
        thread.start()
    while any(thread.is_alive() for thread in threads):
        time.sleep(0.2)


def run(platform, mode, lengths, output_file, custom=None):
    while True:
        stats.pass_no += 1
        names, total = build_names(platform, mode, lengths, custom)
        stats.pass_total = total
        stats.pass_done = 0
        executor = ThreadPoolExecutor(max_workers=platform["workers"])
        if platform["lanes"]:
            scan_bulk(platform, names, output_file, executor)
        else:
            scan_single(platform, names, output_file, executor)
        executor.shutdown(wait=True)
        log(f"{CYAN}  Pass {stats.pass_no} done{RESET}{GRAY} · {stats.found} hits so far · starting over...{RESET}")
        time.sleep(PASS_PAUSE)


def clear():
    os.system("cls" if os.name == "nt" else "clear")


def header(summary=()):
    clear()
    print(CYAN + LOGO + RESET)
    print(f"  {BOLD}USERNAME FINDER{RESET} {GRAY}· Roblox & Minecraft{RESET}")
    print(GRAY + "  " + "─" * 48 + RESET)
    for label, value in summary:
        print(f"  {GRAY}{label:<10}{RESET} {value}")
    if summary:
        print(GRAY + "  " + "─" * 48 + RESET)
    print()


def menu(title, options, summary, back=True, note=""):
    while True:
        header(summary)
        print(f"  {BOLD}{title}{RESET}")
        if note:
            print(f"  {GRAY}{note}{RESET}")
        print()
        for number, (label, hint) in enumerate(options, 1):
            line = f"   {CYAN}[{number}]{RESET} {label}"
            if hint:
                line += f"  {GRAY}{hint}{RESET}"
            print(line)
        if back:
            print(f"   {CYAN}[0]{RESET} {GRAY}Back{RESET}")
        print()
        choice = input(f"  {CYAN}»{RESET} ").strip()
        if back and choice == "0":
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            return int(choice) - 1


def mask_webhook(url):
    return url.rsplit("/", 1)[0] + "/" + "*" * 12


def test_webhook(url):
    payload = {
        "username": "Username Finder",
        "embeds": [{
            "title": "Connected",
            "description": "Alerts will show up here when a username is available.",
            "color": 0x5865F2,
            "footer": {"text": "Username Finder"},
        }],
    }
    try:
        r = requests.post(url, json=payload, timeout=8)
        return r.status_code < 300
    except requests.exceptions.RequestException:
        return False


def prompt_new_webhook(intro):
    while True:
        header()
        print(f"  {BOLD}Discord webhook{RESET}\n")
        print(f"  {intro}\n")
        url = input(f"  {CYAN}»{RESET} ").strip()
        if not url:
            return None
        if not WEBHOOK_RE.match(url):
            print(f"\n  {RED}That doesn't look like a Discord webhook URL.{RESET}")
            time.sleep(2)
            continue
        print(f"\n  {GRAY}Sending a test message...{RESET}")
        if test_webhook(url):
            config["webhook"] = url
            config.pop("webhook_skipped", None)
            save_config(config)
            return url
        print(f"\n  {RED}Discord didn't accept it. Check the URL and try again.{RESET}")
        time.sleep(2)


def first_run_webhook():
    url = prompt_new_webhook(
        "Paste your Discord webhook URL to get alerts, or press Enter to skip.\n"
        "  You can change it later in Settings."
    )
    if url is None:
        config["webhook_skipped"] = True
        save_config(config)


def webhook_menu():
    while True:
        saved = config.get("webhook")
        current = mask_webhook(saved) if saved else "not set"
        index = menu(
            "Discord webhook",
            [
                ("Enter a new webhook", ""),
                ("Send a test message", ""),
                ("Remove webhook", ""),
            ],
            [],
            note=f"Current: {current}",
        )
        if index is None:
            return
        if index == 0:
            prompt_new_webhook("Paste your webhook URL, or press Enter to go back.")
        elif index == 1:
            if not saved:
                print(f"\n  {YELLOW}No webhook set yet.{RESET}")
            elif test_webhook(saved):
                print(f"\n  {GREEN}Test message sent.{RESET}")
            else:
                print(f"\n  {RED}Discord didn't accept the test message.{RESET}")
            time.sleep(1.5)
        else:
            config.pop("webhook", None)
            config["webhook_skipped"] = True
            save_config(config)


def settings_menu():
    speeds = ["safe", "normal", "fast"]
    while True:
        options = [
            ("Discord webhook", "connected" if config.get("webhook") else "off"),
            ("Discord message style", "Embed" if get_setting("message_style") == "embed" else "Plain text"),
            ("Roblox speed", get_setting("roblox_speed").capitalize()),
            ("Minecraft endpoints", "Both (faster)" if get_setting("minecraft_both") else "One"),
            ("Show every name", "Yes" if get_setting("show_all") else "No"),
        ]
        index = menu("Settings", options, [], note="Pick a setting to change it.")
        if index is None:
            return
        if index == 0:
            webhook_menu()
        elif index == 1:
            set_setting("message_style", "plain" if get_setting("message_style") == "embed" else "embed")
        elif index == 2:
            current = speeds.index(get_setting("roblox_speed"))
            set_setting("roblox_speed", speeds[(current + 1) % len(speeds)])
        elif index == 3:
            set_setting("minecraft_both", not get_setting("minecraft_both"))
        elif index == 4:
            set_setting("show_all", not get_setting("show_all"))


def ask_path():
    header()
    print(f"  {BOLD}Import a list{RESET}\n")
    print("  Paste the full path to a .txt file, or press Enter to go back.\n")
    raw = input(f"  {CYAN}»{RESET} ").strip().strip('"').strip("'")
    if not raw:
        return None
    if not os.path.isfile(raw):
        print(f"\n  {RED}File not found.{RESET}")
        time.sleep(2)
        return None
    return raw


def choose_list(platform, summary):
    os.makedirs(LISTS_DIR, exist_ok=True)
    note = "Put .txt files in the 'lists' folder next to main.py. One name per line."
    while True:
        files = sorted(f for f in os.listdir(LISTS_DIR) if f.lower().endswith(".txt"))
        options = [(name, "") for name in files] + [("Enter a file path", "")]
        index = menu("Choose a list", options, summary, note=note)
        if index is None:
            return None
        if index < len(files):
            path = os.path.join(LISTS_DIR, files[index])
        else:
            path = ask_path()
            if path is None:
                continue
        names, skipped, duplicates = load_list(path, platform)
        if not names:
            print(f"\n  {RED}No valid {platform['label']} usernames found in that file.{RESET}")
            time.sleep(2.5)
            continue
        return names, os.path.basename(path), skipped, duplicates


def webhook_line():
    return ("Webhook", f"{GREEN}connected{RESET}" if config.get("webhook") else f"{GRAY}off{RESET}")


def start_scan():
    platforms = build_platforms()
    summary = [webhook_line()]
    keys = ["roblox", "minecraft"]

    minecraft_hint = "a-z 0-9 _ · bulk lookup"
    if len(platforms["minecraft"]["lanes"]) > 1:
        minecraft_hint += ", 2 endpoints"
    index = menu(
        "Choose platform",
        [("Roblox", "a-z 0-9"), ("Minecraft", minecraft_hint)],
        summary,
    )
    if index is None:
        return
    platform = platforms[keys[index]]
    summary.append(("Platform", platform["label"]))

    index = menu(
        "Choose source",
        [
            ("Rare usernames", "pronounceable names, patterns, words.txt"),
            ("All combinations", "every possible name"),
            ("Import .txt list", "check your own names"),
        ],
        summary,
    )
    if index is None:
        return
    mode = ["rare", "all", "list"][index]
    mode_label = ["Rare", "All", "List"][index]
    summary.append(("Mode", mode_label))

    lengths = [3]
    custom = None
    skipped = 0
    duplicates = 0
    if mode == "list":
        picked = choose_list(platform, summary)
        if picked is None:
            return
        custom, list_name, skipped, duplicates = picked
        summary.append(("List", list_name))
    else:
        index = menu(
            "Name length",
            [("3 characters", ""), ("4 characters", ""), ("3 and 4 characters", "")],
            summary,
        )
        if index is None:
            return
        lengths = [[3], [4], [3, 4]][index]
        summary.append(("Length", " and ".join(str(n) for n in lengths)))

    seen.clear()
    filename = platform["file"]
    if os.path.exists(filename):
        with open(filename) as f:
            seen.update(line.strip() for line in f if line.strip())

    header(summary)
    total = build_names(platform, mode, lengths, custom)[1]
    print(f"  {GRAY}Names per pass{RESET}  {fmt_int(total)}")
    if mode == "list" and (skipped or duplicates):
        print(f"  {GRAY}Skipped{RESET}         {skipped} invalid, {duplicates} duplicates")
    if mode == "rare":
        if os.path.exists(WORDS_PATH):
            count = sum(len(load_words(length, platform["charset"])) for length in lengths)
            print(f"  {GRAY}words.txt{RESET}       {fmt_int(count)} matching names")
        else:
            print(f"  {GRAY}words.txt{RESET}       not found (optional, put it next to main.py)")
    print(f"  {GRAY}Results{RESET}         {os.path.basename(filename)}")
    if msvcrt:
        print(f"  {GRAY}Keys{RESET}            {CYAN}P{RESET} pause / resume   {CYAN}Q{RESET} quit")
    else:
        print(f"  {GRAY}Keys{RESET}            Ctrl+C to stop")
    print()
    input(f"  {CYAN}»{RESET} Press Enter to start ")

    webhook_url = config.get("webhook")
    stats.label = platform["label"]
    stats.mode_label = mode_label
    stats.filename = filename
    stats.verbose = get_setting("show_all")
    stats.webhook = bool(webhook_url)
    if platform["lanes"]:
        stats.pacers = [lane["pacer"] for lane in platform["lanes"]]
    else:
        stats.pacers = [platform["pacer"]]
    stats.started = time.time()
    stats.running = True

    print()
    if webhook_url:
        Thread(target=webhook_worker, args=(webhook_url,), daemon=True).start()
    Thread(target=status_loop, daemon=True).start()
    if msvcrt:
        Thread(target=key_listener, daemon=True).start()

    try:
        with open(filename, "a") as output_file:
            run(platform, mode, lengths, output_file, custom)
    except KeyboardInterrupt:
        quit_now()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    init()

    config.update(load_config())
    os.makedirs(LISTS_DIR, exist_ok=True)

    try:
        if not config.get("webhook") and not config.get("webhook_skipped"):
            first_run_webhook()
        while True:
            index = menu(
                "Main menu",
                [("Start scan", ""), ("Settings", ""), ("Quit", "")],
                [webhook_line()],
                back=False,
            )
            if index == 0:
                start_scan()
            elif index == 1:
                settings_menu()
            else:
                break
    except (KeyboardInterrupt, EOFError):
        pass
    print()


if __name__ == "__main__":
    main()
