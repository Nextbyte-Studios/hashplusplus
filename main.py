
import urllib.request
import urllib.error
import zipfile
import io
import shutil
import tempfile
from pathlib import Path, PurePosixPath
import os
import sys
import json
import time
import shlex
import getpass
import math
import importlib.util

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
COMMANDS_DIR = APP_DIR / "commands"

with CONFIG_PATH.open("r", encoding="utf-8") as file:
    config = json.load(file)

commands = {}
usr = getpass.getuser()
_restart_requested = False


def load_hashhash_commands():
    commands.clear()

    if not COMMANDS_DIR.exists():
        print("Warning: 'commands' directory not found. Creating one...")
        COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
        return

    for filename in os.listdir(COMMANDS_DIR):
        if not filename.endswith(".json"):
            continue

        path = COMMANDS_DIR / filename

        try:
            with open(path, "r", encoding="utf-8") as file:
                data = json.load(file)

            required = [
                "name", "triggercommand", "description",
                "usage", "args", "arguments", "script"
            ]

            missing_fields = False

            for f in required:
                if f not in data:
                    print(f"Error in {filename}: missing field '{f}'")
                    missing_fields = True

            if missing_fields:
                continue

            trigger = data["triggercommand"].lower()

            if not trigger or trigger == "help" or trigger in commands:
                print(f"{filename}: invalid or duplicate trigger.")
                continue

            if not isinstance(data["args"], int) or data["args"] < 0:
                print(f"{filename}: args must be a non-negative number.")
                continue

            if not isinstance(data["arguments"], dict):
                print(f"{filename}: arguments must be a JSON object.")
                continue

            if len(data["arguments"]) != data["args"]:
                print(f"{filename}: argument count does not match args.")
                continue

            if not isinstance(data["script"], str):
                print(f"{filename}: script must be text.")
                continue

            commands[trigger] = data

        except json.JSONDecodeError as e:
            print(f"{filename}: invalid JSON on line {e.lineno}.")
        except OSError as e:
            print(f"OS error loading {filename}: {e}")

def restart_hashplusplus():
    global _restart_requested

    print("Restarting HashHashPlus...", flush=True)
    _restart_requested = True

def update_hashplusplus(reloadconfig=False):
    if reloadconfig:
        confirm = input(
            "Running this command resets your config if you have changed it locally, continue?: "
        )

        if confirm.lower() not in ("y", "yes"):
            print("Update cancelled.")
            return

    url = "https://github.com/Nextbyte-Studios/hashhashplus/archive/refs/heads/main.zip"

    print("Checking for updates...")

    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            data = response.read()

        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files = {}

            for name in archive.namelist():
                parts = PurePosixPath(name).parts

                if len(parts) < 2:
                    continue

                relative = Path(*parts[1:])

                if relative.is_absolute() or ".." in relative.parts:
                    continue

                if relative in (Path("main.py"), Path("README.md")):
                    files[relative] = archive.read(name)

                elif relative.parts[0] == "commands" and len(relative.parts) > 1:
                    files[relative] = archive.read(name)

                elif reloadconfig and relative == Path("config.json"):
                    files[relative] = archive.read(name)

        if Path("main.py") not in files or Path("README.md") not in files:
            print("Update failed: required files are missing from GitHub.")
            return

        if reloadconfig and Path("config.json") not in files:
            print("Update failed: config.json is missing from GitHub.")
            return

    except (urllib.error.URLError, TimeoutError, OSError, zipfile.BadZipFile) as e:
        print(f"Update failed: {e}")
        return

    try:
        changed = any(
            not (APP_DIR / path).exists()
            or (APP_DIR / path).read_bytes() != content
            for path, content in files.items()
        )
    except OSError as e:
        print(f"Update failed while checking local files: {e}")
        return

    if not changed:
        answer = input("You are up-to-date, re-install anyways? (Y/N): ")

        if answer.lower() not in ("y", "yes"):
            print("Update cancelled.")
            return

    try:
        with tempfile.TemporaryDirectory(dir=str(APP_DIR)) as temp:
            temp_dir = Path(temp)

            for path, content in files.items():
                destination = temp_dir / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)

            commands_dir = APP_DIR / "commands"
            new_commands = temp_dir / "commands"
            backup_commands = APP_DIR / "commands_backup"

            if new_commands.exists():
                if commands_dir.exists():
                    commands_dir.rename(backup_commands)

                try:
                    shutil.copytree(new_commands, commands_dir)
                except OSError:
                    if backup_commands.exists():
                        if commands_dir.exists():
                            shutil.rmtree(commands_dir)
                        backup_commands.rename(commands_dir)
                    raise

                if backup_commands.exists():
                    shutil.rmtree(backup_commands)

            for filename in ("README.md", "main.py", "config.json"):
                path = Path(filename)

                if path in files:
                    shutil.copy2(temp_dir / path, APP_DIR / path)

        print("Update complete!")
        restart_hashplusplus()

    except OSError as e:
        print(f"Update failed while installing files: {e}")


def command_error(name, **values):
    try:
        message = config["errors"][name]
        print(message.format(**values))
    except Exception:
        print(f"An error occurred: {name}")


def hashhash_help(args):
    if len(args) > 1:
        print("Usage: help [page or command]")
        return

    if not args:
        hashhash_help_page(1)
        return

    target = args[0].lower()

    if target.isdigit():
        hashhash_help_page(int(target))
        return

    command = commands.get(target)

    if command is None:
        command_error("unknown_command", userinput=target)
        return

    print(f"\n{command['name']}")
    print(command["description"])
    print(f"Usage: {command['usage']}")
    print(f"Arguments: {command['args']}")


def hashhash_help_page(page):
    available = list(commands.values())
    total = max(1, (len(available) + 9) // 10)

    if page < 1 or page > total:
        print("Page does not exist.")
        return

    start_idx = (page - 1) * 10
    end_idx = page * 10

    for command in available[start_idx:end_idx]:
        print(f"{command['triggercommand']} - {command['description']}")

    if total > 1:
        print(f"\nPage {page}/{total}")


def run_hashhash_command(userinput):
    try:
        parts = shlex.split(userinput)
    except ValueError:
        return

    if not parts:
        return

    trigger = parts[0].lower()
    args = parts[1:]

    if trigger == "restart":
        if args:
            print("Usage: restart")
            return

        restart_hashplusplus()
        return

    if trigger == "update":
        if len(args) > 1 or (args and args[0].lower() != "reloadconfig"):
            print("Usage: update [reloadconfig]")
            return

        update_hashplusplus(reloadconfig=bool(args))
        return

    if trigger == "help":
        hashhash_help(args)
        return

    command = commands.get(trigger)

    if command is None:
        command_error("unknown_command", userinput=userinput)
        return

    required = command["args"]

    if len(args) < required:
        command_error(
            "not_enough_args",
            usercommand=trigger,
            args=required,
            userargs=len(args)
        )
        return

    if len(args) > required:
        command_error(
            "too_many_args",
            userinput=userinput,
            args=len(args),
            command=trigger,
            correctedargs=required
        )
        return

    variables = {
        "os": os,
        "sys": sys,
        "time": time,
        "json": json,
        "print": print
    }

    idx = 0

    for var_name in command["arguments"].values():
        variables[var_name] = args[idx]
        idx += 1

    try:
        exec(command["script"], variables)
    except Exception as e:
        command_error("script_fail", userinput=userinput)
        print(f"Details: {type(e).__name__}: {e}")


def main():
    print(config.get("startmessage", "Welcome to HashHashPlus!"))

    load_hashhash_commands()

    last_interrupt = 0

    while True:
        try:
            userinput = input(
                f"\n(hashhashplus) {getpass.getuser()} > "
            ).strip()

            if not userinput:
                continue

            run_hashhash_command(userinput)
            if _restart_requested:
                return True

        except KeyboardInterrupt:
            now = time.monotonic()

            if now - last_interrupt <= 1.5:
                print("\nGoodbye!")
                sys.exit(0)

            force_quit_msg = config.get("errors", {}).get(
                "force_quit",
                "Press Ctrl+C again to exit."
            )

            print("\n" + force_quit_msg)
            last_interrupt = now

        except EOFError:
            print("\nGoodbye!")
            sys.exit(0)


if __name__ == "__main__":
    if os.name == "nt":
        os.system("cls")
    else:
        os.system("clear")

    runtime = sys.modules[__name__]

    while runtime.main():
        module_name = "_hashhashplus_runtime"
        spec = importlib.util.spec_from_file_location(
            module_name,
            APP_DIR / "main.py",
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Unable to reload application from {APP_DIR / 'main.py'}")

        runtime = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = runtime
        spec.loader.exec_module(runtime)
