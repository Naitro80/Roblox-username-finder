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
from itertools import islice
from threading import Lock, Thread
from colorama import Back, Fore, Style, init

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

WEBHOOK_RE = re.compile(
    r"^https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/[\w-]+$"
)

LETTERS = string.ascii_lowercase
ALNUM = LETTERS + string.digits
CONSONANTS = "bcdfghjklmnpqrstvwxyz"
VOWELS = "aeiou"

WORKERS = 8
CHUNK_SIZE = 200
PASS_PAUSE = 5
MAX_COOLDOWN = 120.0

ROBLOX_CHECK_URL = "https://auth.roblox.com/v1/usernames/validate?Username={}&Birthday=2000-01-01"
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

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
})


class RateLimited(Exception):
    def __init__(self, retry_after=None):
        super().__init__("rate limited")
        self.retry_after = retry_after


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


PLATFORMS = {
    "roblox": {
        "label": "Roblox",
        "charset": ALNUM,
        "check": roblox_check,
        "file": os.path.join(BASE_DIR, "available_roblox.txt"),
        "pacer": Pacer(0.08, 0.04, 10.0, 5.0),
    },
    "minecraft": {
        "label": "Minecraft",
        "charset": ALNUM + "_",
        "check": minecraft_check,
        "file": os.path.join(BASE_DIR, "available_minecraft.txt"),
        "pacer": Pacer(1.1, 1.0, 15.0, 15.0),
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


def build_names(platform, mode, lengths):
    charset = platform["charset"]
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
    pause = max((p.pause_left() for p in stats.pacers), default=0.0)

    parts = [
        (f" {stats.label.upper()} · {stats.mode_label.upper()} ", Back.CYAN + Fore.BLACK),
        (f"{bar} {pct * 100:5.1f}%", CYAN),
    ]
    if pause >= 1:
        parts.append((f"pause {int(pause)}s", YELLOW))
    parts.append((f"hits {stats.found}", GREEN if stats.found else GRAY))
    parts.append((f"{stats.rate:.1f}/s", RESET))
    parts.append((f"{fmt_int(stats.checked)} checked", RESET))
    parts.append((f"pass {stats.pass_no}", RESET))
    if stats.current:
        parts.append((f"now {stats.current}", GRAY))
    if stats.limited:
        parts.append((f"429 x{stats.limited}", YELLOW))
    remaining = stats.pass_total - stats.pass_done
    if stats.rate > 0 and remaining > 0:
        parts.append((f"ETA {fmt_duration(remaining / stats.rate)}", GRAY))
    parts.append((f"time {fmt_duration(time.time() - stats.started)}", GRAY))

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


def webhook_worker(url):
    while True:
        label, name = notify_queue.get()
        payload = {"content": f"Available - {name} ({label})"}
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
            labels = {"taken": (GRAY, "TAKEN"), "blocked": (RED, "BLOCKED"), "invalid": (YELLOW, "INVALID")}
            color, text = labels[status]
            log(f"{color}  {text:<8}{name}{RESET}")
        return


def run(platform, mode, lengths, output_file):
    while True:
        stats.pass_no += 1
        names, total = build_names(platform, mode, lengths)
        stats.pass_total = total
        stats.pass_done = 0
        executor = ThreadPoolExecutor(max_workers=WORKERS)
        for chunk in chunked(names, CHUNK_SIZE):
            list(executor.map(lambda name: check_name(platform, name, output_file), chunk))
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


def menu(title, options, summary):
    while True:
        header(summary)
        print(f"  {BOLD}{title}{RESET}\n")
        for number, (label, hint) in enumerate(options, 1):
            line = f"   {CYAN}[{number}]{RESET} {label}"
            if hint:
                line += f"  {GRAY}{hint}{RESET}"
            print(line)
        print()
        choice = input(f"  {CYAN}»{RESET} ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            return int(choice) - 1


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(config):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except OSError:
        pass


def mask_webhook(url):
    return url.rsplit("/", 1)[0] + "/" + "*" * 12


def test_webhook(url):
    try:
        r = requests.post(url, json={"content": "Username Finder connected"}, timeout=8)
        return r.status_code < 300
    except requests.exceptions.RequestException:
        return False


def setup_webhook():
    config = load_config()
    saved = config.get("webhook", "")
    if saved and WEBHOOK_RE.match(saved):
        while True:
            header()
            print(f"  {BOLD}Discord webhook{RESET}\n")
            print(f"  Saved: {GRAY}{mask_webhook(saved)}{RESET}\n")
            print(f"   {CYAN}[Enter]{RESET} use it   {CYAN}[n]{RESET} enter a new one   {CYAN}[s]{RESET} skip\n")
            choice = input(f"  {CYAN}»{RESET} ").strip().lower()
            if choice == "":
                return saved
            if choice == "s":
                return None
            if choice == "n":
                break

    while True:
        header()
        print(f"  {BOLD}Discord webhook{RESET}\n")
        print("  Paste your webhook URL, or press Enter to skip.\n")
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
            save_config(config)
            return url
        print(f"\n  {RED}Discord didn't accept it. Check the URL and try again.{RESET}")
        time.sleep(2)


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


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    init()

    webhook_url = setup_webhook()
    summary = [("Webhook", f"{GREEN}connected{RESET}" if webhook_url else f"{GRAY}off{RESET}")]

    keys = ["roblox", "minecraft"]
    index = menu(
        "Choose platform",
        [("Roblox", "a-z 0-9"), ("Minecraft", "a-z 0-9 _")],
        summary,
    )
    platform = PLATFORMS[keys[index]]
    summary.append(("Platform", platform["label"]))

    index = menu(
        "Choose mode",
        [
            ("Rare usernames", "pronounceable names, patterns like abab/aabb, words.txt"),
            ("All combinations", "every possible name"),
        ],
        summary,
    )
    mode = ["rare", "all"][index]
    mode_label = ["Rare", "All"][index]
    summary.append(("Mode", mode_label))

    index = menu(
        "Name length",
        [("3 characters", ""), ("4 characters", ""), ("3 and 4 characters", "")],
        summary,
    )
    lengths = [[3], [4], [3, 4]][index]
    summary.append(("Length", " and ".join(str(n) for n in lengths)))

    index = menu(
        "Display",
        [
            ("Clean live status", "recommended"),
            ("Show every checked name", "scrolls fast"),
        ],
        summary,
    )
    stats.verbose = index == 1

    filename = platform["file"]
    if os.path.exists(filename):
        with open(filename) as f:
            seen.update(line.strip() for line in f if line.strip())

    header(summary)
    total = build_names(platform, mode, lengths)[1]
    print(f"  {GRAY}Names per pass{RESET}  {fmt_int(total)}")
    if mode == "rare":
        if os.path.exists(WORDS_PATH):
            count = sum(len(load_words(length, platform["charset"])) for length in lengths)
            print(f"  {GRAY}words.txt{RESET}       {fmt_int(count)} matching names")
        else:
            print(f"  {GRAY}words.txt{RESET}       not found (optional, put it next to main.py)")
    print(f"  {GRAY}Results{RESET}         {os.path.basename(filename)}")
    print(f"  {GRAY}Press Ctrl+C to stop{RESET}\n")

    stats.label = platform["label"]
    stats.mode_label = mode_label
    stats.webhook = bool(webhook_url)
    stats.pacers = [platform["pacer"]]
    stats.started = time.time()
    stats.running = True

    if webhook_url:
        Thread(target=webhook_worker, args=(webhook_url,), daemon=True).start()
    Thread(target=status_loop, daemon=True).start()

    try:
        with open(filename, "a") as output_file:
            run(platform, mode, lengths, output_file)
    except KeyboardInterrupt:
        stats.running = False
        print_exit_summary(filename)
        os._exit(0)


if __name__ == "__main__":
    main()
