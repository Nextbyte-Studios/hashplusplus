import os
import sys
import json
import time
import shlex
import getpass

with open("config.json", "r", encoding="utf-8") as file:
    config = json.load(file)

commands = {}
usr = getpass.getuser()

def load_hashhash_commands():
    commands.clear()

    if not os.path.exists("commands"):
        print("Warning: 'commands' directory not found. Creating one...")
        os.makedirs("commands")
        return

    for filename in os.listdir("commands"):
        if not filename.endswith(".json"):
            continue

        path = os.path.join("commands", filename)

        try:
            with open(path, "r", encoding="utf-8") as file:
                data = json.load(file)

            required = ["name", "triggercommand", "description",
                        "usage", "args", "arguments", "script"]

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
    if os.name == "nt":
        os.system("cls")
    else:
        os.system("clear")

    print(config.get("startmessage", "Welcome to HashHashPlus!"))

    load_hashhash_commands()

    last_interrupt = 0

    while True:
        try:
            userinput = input(f"\n(hashhashplus) {getpass.getuser()} > ").strip()
            if not userinput:
                continue
            run_hashhash_command(userinput)

        except KeyboardInterrupt:
            now = time.monotonic()

            if now - last_interrupt <= 1.5:
                print("\nGoodbye!")
                sys.exit(0)

            force_quit_msg = config.get("errors", {}).get("force_quit", "Press Ctrl+C again to exit.")
            print("\n" + force_quit_msg)
            last_interrupt = now


if __name__ == "__main__":
    main()