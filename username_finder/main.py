import requests
import time
import itertools
import string
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from colorama import Fore, Style, init

init()

LOGO = r"""
███╗░░██╗██╗░░██╗
████╗░██║██║░░██║
██╔██╗██║███████║
██║╚████║██╔══██║
██║░╚███║██║░░██║
╚═╝░░╚══╝╚═╝░░╚═╝
"""

CHARSET = string.ascii_lowercase + string.digits
WORKERS = 4
REQUEST_DELAY = 0.15

write_lock = Lock()
session = requests.Session()

def check_username(username, output_file, retries=3):
    url = f"https://auth.roblox.com/v1/usernames/validate?Username={username}&Birthday=2000-01-01"
    time.sleep(REQUEST_DELAY)
    for attempt in range(retries):
        try:
            response = session.get(url, timeout=5)

            if response.status_code == 429:
                wait = 2 ** attempt
                print(Fore.YELLOW + f"rate limited, backing off {wait}s ({username})" + Style.RESET_ALL)
                time.sleep(wait)
                continue

            response_data = response.json()
            code = response_data.get("code")

            if code == 0:
                print(Fore.GREEN + f"VALID: {username}" + Style.RESET_ALL)
                with write_lock:
                    output_file.write(username + "\n")
                    output_file.flush()
            elif code == 1:
                print(Fore.LIGHTBLACK_EX + f"TAKEN: {username}" + Style.RESET_ALL)
            elif code == 2:
                print(Fore.RED + f"CENSORED: {username}" + Style.RESET_ALL)
            else:
                print(Fore.YELLOW + f"bruh ({code}): {username}" + Style.RESET_ALL)
            return

        except requests.exceptions.RequestException as e:
            print(Fore.YELLOW + f"glitch {username}: {e}" + Style.RESET_ALL)
            return

    print(Fore.YELLOW + f"gave up on {username} after {retries} retries" + Style.RESET_ALL)

def generate_usernames(length):
    for combo in itertools.product(CHARSET, repeat=length):
        yield "".join(combo)

def choose_length():
    while True:
        print(Fore.CYAN + "1. 3-letter usernames" + Style.RESET_ALL)
        print(Fore.CYAN + "2. 4-letter usernames" + Style.RESET_ALL)
        choice = input("Choose an option: ").strip()
        if choice == "1":
            return 3
        elif choice == "2":
            return 4
        else:
            print(Fore.YELLOW + "Invalid choice, try again." + Style.RESET_ALL)

def main():
    print(Fore.CYAN + LOGO + Style.RESET_ALL)

    length = choose_length()

    with open("available.txt", "a") as output_file:
        while True:
            with ThreadPoolExecutor(max_workers=WORKERS) as executor:
                for username in generate_usernames(length):
                    executor.submit(check_username, username, output_file)
            print(Fore.CYAN + "Finished a full pass, starting over..." + Style.RESET_ALL)
            time.sleep(3)

if __name__ == "__main__":
    main()