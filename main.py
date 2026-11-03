
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
import keyword
import urllib.parse

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
COMMANDS_DIR = APP_DIR / "commands"

with CONFIG_PATH.open("r", encoding="utf-8") as file:
    config = json.load(file)

commands = {}
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


def _normalize_repository_reference(reference):
    value = reference.strip()
    if value.startswith("git@github.com:"):
        value = "https://github.com/" + value[len("git@github.com:"):]
    elif value.startswith("github.com/"):
        value = "https://" + value
    elif "://" not in value:
        value = "https://github.com/" + value

    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or parsed.hostname not in ("github.com", "www.github.com")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Use a GitHub repository link or owner/repository.")

    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) != 2:
        raise ValueError("Use a GitHub repository link or owner/repository.")

    owner, repository = parts
    if repository.lower().endswith(".git"):
        repository = repository[:-4]

    valid_part = lambda part: (
        part not in ("", ".", "..")
        and all(character.isalnum() or character in "._-" for character in part)
    )
    if not valid_part(owner) or not valid_part(repository):
        raise ValueError("The GitHub repository name is invalid.")

    return owner, repository, f"https://github.com/{owner}/{repository}"


def _validate_package_command(data, filename):
    if not isinstance(data, dict):
        raise ValueError(f"{filename}: command JSON must contain an object.")

    required = (
        "name", "triggercommand", "description", "usage",
        "args", "arguments", "script"
    )
    missing = [field for field in required if field not in data]
    if missing:
        raise ValueError(
            f"{filename}: missing required field(s): {', '.join(missing)}."
        )

    for field in ("name", "triggercommand", "description", "usage", "script"):
        if not isinstance(data[field], str):
            raise ValueError(f"{filename}: '{field}' must be text.")

    trigger = data["triggercommand"].strip().lower()
    if not trigger or trigger == "help":
        raise ValueError(f"{filename}: triggercommand is empty or reserved.")

    argument_count = data["args"]
    if (
        not isinstance(argument_count, int)
        or isinstance(argument_count, bool)
        or argument_count < 0
    ):
        raise ValueError(f"{filename}: 'args' must be a non-negative integer.")

    arguments = data["arguments"]
    if not isinstance(arguments, dict):
        raise ValueError(f"{filename}: 'arguments' must be an object.")

    expected_keys = {f"arg{index}" for index in range(1, argument_count + 1)}
    if set(arguments) != expected_keys:
        raise ValueError(
            f"{filename}: 'arguments' must contain exactly "
            f"{', '.join(sorted(expected_keys)) or 'no keys'}."
        )

    values = list(arguments.values())
    if any(
        not isinstance(value, str)
        or not value.isidentifier()
        or keyword.iskeyword(value)
        for value in values
    ):
        raise ValueError(
            f"{filename}: each argument name must be a valid Python identifier."
        )
    if len(set(values)) != len(values):
        raise ValueError(f"{filename}: argument names must be unique.")


def _fetch_repository_commands(reference):
    owner, repository, _ = _normalize_repository_reference(reference)
    api_url = (
        "https://api.github.com/repos/"
        f"{urllib.parse.quote(owner, safe='')}/"
        f"{urllib.parse.quote(repository, safe='')}"
    )
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "HashPlusPlus",
    }

    with urllib.request.urlopen(
        urllib.request.Request(api_url, headers=headers), timeout=20
    ) as response:
        repository_data = json.loads(response.read().decode("utf-8"))

    if not isinstance(repository_data, dict):
        raise ValueError("GitHub returned invalid repository information.")

    full_name = repository_data.get("full_name")
    default_branch = repository_data.get("default_branch")
    if not isinstance(full_name, str) or not isinstance(default_branch, str):
        raise ValueError("GitHub returned incomplete repository information.")

    _, _, source = _normalize_repository_reference(full_name)
    archive_url = (
        "https://api.github.com/repos/"
        f"{urllib.parse.quote(full_name, safe='/')}/zipball/"
        f"{urllib.parse.quote(default_branch, safe='')}"
    )
    with urllib.request.urlopen(
        urllib.request.Request(archive_url, headers=headers), timeout=30
    ) as response:
        archive_data = response.read()

    try:
        archive = zipfile.ZipFile(io.BytesIO(archive_data))
    except zipfile.BadZipFile as error:
        raise ValueError("GitHub returned an invalid repository archive.") from error

    files = {}
    triggers = set()
    with archive:
        for entry in archive.infolist():
            path = PurePosixPath(entry.filename)
            if (
                path.is_absolute()
                or ".." in path.parts
                or len(path.parts) != 3
                or path.parts[1] != "commands"
                or path.suffix.lower() != ".json"
                or entry.is_dir()
            ):
                continue

            filename = path.name
            if filename in files:
                raise ValueError(f"The repository contains duplicate {filename} files.")

            try:
                data = json.loads(archive.read(entry).decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError(f"{filename}: command file is not valid UTF-8 JSON.") from error

            _validate_package_command(data, filename)
            trigger = data["triggercommand"].strip().lower()
            if trigger in triggers:
                raise ValueError(f"{filename}: duplicate triggercommand '{trigger}'.")
            triggers.add(trigger)

            data["external"] = True
            data["source"] = source
            files[filename] = (
                json.dumps(data, ensure_ascii=False, indent=4) + "\n"
            ).encode("utf-8")

    if not files:
        raise ValueError("The repository has no JSON command files in its commands/ folder.")

    return source, files


def _read_installed_command_records():
    records = []
    if not COMMANDS_DIR.exists():
        return records

    for path in COMMANDS_DIR.glob("*.json"):
        try:
            with path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError) as error:
            print(f"Warning: unable to read {path.name}: {error}")
            continue

        if isinstance(data, dict):
            records.append((path, data))

    return records


def _record_source(data):
    if data.get("external") is not True or not isinstance(data.get("source"), str):
        return None
    try:
        return _normalize_repository_reference(data["source"])[2].lower()
    except ValueError:
        return None


def _matching_external_records(package, records):
    try:
        source = _normalize_repository_reference(package)[2].lower()
    except ValueError:
        if "://" in package or "/" in package or package.startswith("git@"):
            raise
        source = None

    if source is not None:
        return [
            record for record in records
            if _record_source(record[1]) == source
        ]

    target = package.casefold()
    matches = []
    for path, data in records:
        record_source = _record_source(data)
        source_name = (
            record_source.rstrip("/").rsplit("/", 1)[-1]
            if record_source is not None else ""
        )
        if (
            data.get("external") is True
            and (
                str(data.get("name", "")).casefold() == target
                or str(data.get("triggercommand", "")).casefold() == target
                or path.stem.casefold() == target
                or source_name.casefold() == target
            )
        ):
            matches.append((path, data))
    return matches


def _check_repository_file_conflicts(source, files, records):
    same_source = [
        (path, data) for path, data in records
        if _record_source(data) == source.lower()
    ]
    same_source_paths = {path.name for path, _ in same_source}
    other_records = [
        (path, data) for path, data in records
        if _record_source(data) != source.lower()
    ]

    for filename, content in files.items():
        destination = COMMANDS_DIR / filename
        if destination.exists() and filename not in same_source_paths:
            raise ValueError(
                f"Cannot install {filename}: a local command file already uses that name."
            )

        incoming = json.loads(content.decode("utf-8"))
        trigger = incoming["triggercommand"].casefold()
        for _, existing in other_records:
            if str(existing.get("triggercommand", "")).casefold() == trigger:
                raise ValueError(
                    f"Cannot install {filename}: triggercommand '{trigger}' "
                    "is already used by another command."
                )

    return same_source


def _same_repository_commands(current_files, incoming_files):
    if set(current_files) != set(incoming_files):
        return False

    try:
        return all(
            json.loads(current_files[filename].decode("utf-8"))
            == json.loads(incoming_files[filename].decode("utf-8"))
            for filename in incoming_files
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False


def _write_repository_commands(source, files, records):
    same_source = _check_repository_file_conflicts(source, files, records)
    incoming_names = set(files)
    COMMANDS_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix=".hashpm-", dir=str(COMMANDS_DIR)
    ) as temporary_directory:
        staging = Path(temporary_directory)
        for filename, content in files.items():
            (staging / filename).write_bytes(content)

        for filename in files:
            os.replace(staging / filename, COMMANDS_DIR / filename)

    for path, _ in same_source:
        if path.name not in incoming_names and path.exists():
            path.unlink()


def _warn_external_package():
    answer = input(
        "This is an external command package. Please review the scripts before "
        "installing and confirm that you want to continue: "
    )
    return answer.strip().lower() in ("y", "yes")


def _confirm_uninstall(prompt):
    while True:
        answer = input(f"{prompt} (Y/N): ").strip().casefold()
        if answer in ("y", "yes", "continue", "c"):
            return True
        if answer in ("n", "no", "cancel"):
            return False
        print("Please answer Y or N.")


def _install_package(package):
    source, files = _fetch_repository_commands(package)
    records = _read_installed_command_records()
    existing = _check_repository_file_conflicts(source, files, records)

    if existing:
        current_files = {
            path.name: path.read_bytes()
            for path, _ in existing
        }
        if _same_repository_commands(current_files, files):
            print("Package is already downloaded.")
            return
        answer = input("Package is already downloaded. Update it now? (Y/N): ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Install cancelled.")
            return
    elif any(
        path.name in files
        for path, _ in records
    ):
        raise ValueError("A local command file already uses a package filename.")

    if not _warn_external_package():
        print("Install cancelled.")
        return

    _write_repository_commands(source, files, records)
    print("Package installed. Use 'restart' to use it, 'update' may overwrite it.")
    load_hashhash_commands()


def _installed_sources_for_package(package, records):
    matches = _matching_external_records(package, records)
    sources = sorted({
        _record_source(data)
        for _, data in matches
        if _record_source(data) is not None
    })
    return sources


def _update_package(package):
    records = _read_installed_command_records()
    sources = _installed_sources_for_package(package, records)
    if not sources:
        print("Package not found.")
        return
    if len(sources) > 1:
        print("Package name matches multiple repositories:")
        for source in sources:
            print(source)
        print("Use a repository link to select one.")
        return

    source, files = _fetch_repository_commands(sources[0])
    existing = _check_repository_file_conflicts(source, files, records)
    current_files = {
        path.name: path.read_bytes()
        for path, _ in existing
    }
    if _same_repository_commands(current_files, files):
        answer = input("Nothing to update, reinstall anyway? (Y/N): ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Update cancelled.")
            return

    if not _warn_external_package():
        print("Update cancelled.")
        return

    _write_repository_commands(source, files, records)
    print("Package updated. Use 'restart' to use it, 'update' may overwrite it.")
    load_hashhash_commands()


def _uninstall_package(package):
    records = _read_installed_command_records()
    external_matches = _matching_external_records(package, records)

    if external_matches:
        matches = external_matches
    else:
        try:
            _normalize_repository_reference(package)
            print("Package not found.")
            return
        except ValueError:
            if "://" in package or "/" in package or package.startswith("git@"):
                raise

        target = package.casefold()
        matches = [
            (path, data) for path, data in records
            if (
                data.get("external") is not True
                and (
                    str(data.get("name", "")).casefold() == target
                    or str(data.get("triggercommand", "")).casefold() == target
                    or path.stem.casefold() == target
                )
            )
        ]
        if matches:
            if not _confirm_uninstall(
                "This package is marked as built-in, delete anyway?"
            ):
                print("Uninstall cancelled.")
                return

    if not matches:
        print("Package not found.")
        return

    command_names = [
        str(data.get("name") or data.get("triggercommand") or path.stem)
        for path, data in matches
    ]
    if not _confirm_uninstall(
        "This will delete the following commands: "
        f"{', '.join(command_names)}. Continue?"
    ):
        print("Uninstall cancelled.")
        return

    for path, _ in matches:
        path.unlink()

    print("Package uninstalled.")
    load_hashhash_commands()


def _list_packages():
    local = []
    external = {}
    for command in commands.values():
        name = str(command.get("name") or command.get("triggercommand", ""))
        trigger = str(command.get("triggercommand", ""))
        label = f"{name} ({trigger})" if trigger and name != trigger else name
        source = _record_source(command)
        if source is None:
            local.append(label)
        else:
            external.setdefault(command["source"], []).append(label)

    print("You have the following packages:")
    print("Built-in / local:")
    print("\n".join(f"- {item}" for item in sorted(local)) or "- (none)")
    print("External:")
    if not external:
        print("- (none)")
        return
    for source in sorted(external, key=str.casefold):
        print(f"{source}:")
        print("\n".join(f"- {item}" for item in sorted(external[source])))


def hashpm_manager(action, package=None):
    action = action.lower()
    if action == "list":
        _list_packages()
        return
    if action not in ("install", "uninstall", "update"):
        print("Usage: hashpm <install|uninstall|update|list> [repo/package]")
        return
    if not package:
        print("Usage: hashpm <install|uninstall|update|list> [repo/package]")
        return

    try:
        if action == "install":
            _install_package(package)
        elif action == "uninstall":
            _uninstall_package(package)
        else:
            _update_package(package)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            print("Repository not found.")
        else:
            print(f"Package operation failed: {error}")
    except (
        OSError, urllib.error.URLError, TimeoutError, ValueError,
        json.JSONDecodeError
    ) as error:
        print(f"Package operation failed: {error}")


def restart_hashplusplus():
    global _restart_requested

    print("Restarting HashHashPlus...", flush=True)
    _restart_requested = True

def update_hashplusplus(reloadconfig=False):
    if reloadconfig:
        confirm = input(
            "This will reset your local config changes. Continue? "
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

    if trigger == "hashpm":
        if not args:
            print("Usage: hashpm <install|uninstall|update|list> [repo/package]")
            return

        action = args[0].lower()
        if action == "list":
            if len(args) != 1:
                print("Usage: hashpm list")
                return
        elif action in ("install", "uninstall", "update"):
            if len(args) != 2:
                print(f"Usage: hashpm {action} <repo/package>")
                return
        else:
            print("Usage: hashpm <install|uninstall|update|list> [repo/package]")
            return

        variables = {
            "hashpm_manager": hashpm_manager,
            "action": action,
            "package": args[1] if len(args) == 2 else None,
        }
        try:
            exec(command["script"], variables)
        except Exception as e:
            command_error("script_fail", userinput=userinput)
            print(f"Details: {type(e).__name__}: {e}")
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
    print(config.get("startmessage"))

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

            if now - last_interrupt <= 0.5:
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
    if config.get("clearterminalonstart", False):
        os.system("cls" if os.name == "nt" else "clear")

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
