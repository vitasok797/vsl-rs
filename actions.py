from __future__ import annotations

import argparse
import ast
import colorama
import inspect
import locale
import os
import re
import subprocess
import sys
import traceback
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Any

IS_WINDOWS = (sys.platform == 'win32')
ROOT_DIR = Path(__file__).parent.resolve()
PROFILES = ['dev', 'release', 'dev-static', 'release-static']
DEFAULT_PROFILE = 'dev'


class Color(Enum):
    BLACK = colorama.Fore.BLACK
    RED = colorama.Fore.RED
    GREEN = colorama.Fore.GREEN
    YELLOW = colorama.Fore.YELLOW
    BLUE = colorama.Fore.BLUE
    MAGENTA = colorama.Fore.MAGENTA
    CYAN = colorama.Fore.CYAN
    WHITE = colorama.Fore.WHITE
    RESET = colorama.Fore.RESET


def colored(text: str, color: Color) -> str:
    return color.value + text + colorama.Style.RESET_ALL


def print_err(text: str, **kwargs: Any) -> None:
    print(colored(text, Color.RED), file=sys.stderr, **kwargs)


def print_hr() -> None:
    SYMBOL = '-'
    WIDTH = 99
    print(SYMBOL * WIDTH)


def get_os_specific_exe_name(name: str) -> str:
    return name + ('.exe' if IS_WINDOWS else '')


def get_os_specific_script_name(name: str) -> str:
    return name + ('.bat' if IS_WINDOWS else '.sh')


class CommandParseError(Exception):
    pass


class ActionError(Exception):
    pass


class ActionCancel(Exception):
    pass


class Console:
    CONSOLE_ENCODING = 'oem' if IS_WINDOWS else locale.getpreferredencoding()

    class ShellCommandError(Exception):
        pass

    class GetEnvError(Exception):
        pass

    class ShellCommandOutput(Enum):
        DEFAULT = auto()
        SUPPRESS = auto()
        CAPTURE = auto()
        CAPTURE_COMBINED = auto()

    @dataclass
    class ShellCommandResult:
        exit_code: int
        stdout: str
        stderr: str

    @staticmethod
    def run_shell_command(
            command: str,
            *,
            cwd: Path | None = None,
            env: Mapping[str, str] | None = None,
            check_exit_code: bool = True,
            error_exception: type[Exception] = ShellCommandError,
            output: ShellCommandOutput = ShellCommandOutput.DEFAULT,
            ) -> ShellCommandResult:
        output_options: dict[str, int | None] = {}
        if output == Console.ShellCommandOutput.DEFAULT:
            output_options['stdout'] = None
            output_options['stderr'] = None
        elif output == Console.ShellCommandOutput.SUPPRESS:
            output_options['stdout'] = subprocess.DEVNULL
            output_options['stderr'] = subprocess.DEVNULL
        elif output == Console.ShellCommandOutput.CAPTURE:
            output_options['stdout'] = subprocess.PIPE
            output_options['stderr'] = subprocess.PIPE
        elif output == Console.ShellCommandOutput.CAPTURE_COMBINED:
            output_options['stdout'] = subprocess.PIPE
            output_options['stderr'] = subprocess.STDOUT
        else:
            raise ValueError(f'Unknown "output" argument value ({output})')

        completed_process = subprocess.run(
                command,
                cwd=cwd,
                env=env,
                shell=True,
                check=False,
                encoding=Console.CONSOLE_ENCODING,
                **output_options,
                )

        stdout = completed_process.stdout or ''
        stderr = completed_process.stderr or ''

        if check_exit_code and completed_process.returncode != 0:
            msg = f'({completed_process.returncode})'
            stderr_msg = stderr.strip()
            msg += f' {stderr_msg}' if stderr_msg else ''
            raise error_exception(msg)

        return Console.ShellCommandResult(
                exit_code=completed_process.returncode,
                stdout=stdout.strip(),
                stderr=stderr.strip(),
                )

    @staticmethod
    def get_shell_script_environ(script_file: Path | str) -> dict[str, str]:
        if IS_WINDOWS:
            command = f'"{script_file}" > nul && set'
        else:
            command = f'. "{script_file}" > /dev/null && env'

        result = subprocess.run(
                command,
                shell=True,
                check=False,
                capture_output=True,
                encoding=Console.CONSOLE_ENCODING,
                )
        if result.returncode != 0:
            error_message = f'Error getting environment ({result.stderr.strip()})'
            raise Console.GetEnvError(error_message)

        records = re.findall(r'^(.*?)=(.*)$', result.stdout.strip(), re.MULTILINE)
        return dict(records)

    @staticmethod
    def input_string(prompt: str, *, allow_empty: bool = True) -> str:
        while True:
            print(f'{prompt} ', end='', flush=True)
            text = input()
            if text or allow_empty:
                return text

    @staticmethod
    def input_int(prompt: str) -> int | None:
        while True:
            text = Console.input_string(prompt)
            if not text:
                return None
            try:
                return int(text)
            except ValueError:
                pass

    @staticmethod
    def choose_yn(prompt: str, *, default: bool | None = None) -> bool:
        while True:
            text = Console.input_string(prompt)
            if not text and default is not None:
                return default
            if text.lower() in ['y', 'yes']:
                return True
            if text.lower() in ['n', 'no']:
                return False

    @staticmethod
    def select(
            prompt: str,
            variants: dict[str, Any],
            ) -> Any | None:  # noqa: ANN401
        for i, variant in enumerate(variants, start=1):
            print(f'{i}: {variant}')
        while True:
            user_input = Console.input_int(prompt)
            if user_input is None:
                return None
            index = user_input - 1
            if 0 <= index < len(variants):
                values = list(variants.values())
                return values[index]

    @staticmethod
    def pause(desc: str) -> None:
        Console.input_string(f'Press Enter to {desc}...')

    @staticmethod
    def clear() -> None:
        os.system('cls' if IS_WINDOWS else 'clear')


class BaseAction:
    PROMPT_SHORTCUT: str | None = None
    CLI_COMMAND: str | None = None
    DESC: str | None = None
    ARGS: str | None = None

    def __init__(self) -> None:
        self.manager = None

    def cli_add_subparser(self, subparsers) -> None:  # noqa: ANN001
        if not self.CLI_COMMAND:
            return
        subparser = subparsers.add_parser(self.CLI_COMMAND)
        subparser.set_defaults(func=self.execute)
        self._cli_add_args(subparser)

    def _cli_add_args(self, subparser: argparse.ArgumentParser) -> None:
        pass

    def is_active(self) -> bool:
        return True

    def execute(self, **kwargs: Any) -> None:
        pass


class BaseActionManager:
    def __init__(self) -> None:
        self._init_actions()
        self.profiles = PROFILES
        self.active_profile = DEFAULT_PROFILE

    def _init_actions(self) -> None:
        actions = [ActionClass() for ActionClass in BaseAction.__subclasses__()]
        self.actions = [action for action in actions if action.is_active()]

        for action in self.actions:
            action.manager = self

    def start(self) -> None:
        pass


class InteractiveActionManager(BaseActionManager):
    def start(self) -> None:
        while True:
            self._process_user_command()

    def _process_user_command(self) -> None:
        print(self._generate_prompt(), end='', flush=True)

        user_input = input().strip()
        if not user_input:
            return

        try:
            actions = self._parse_command(user_input)
        except CommandParseError as ex:
            print_err(str(ex))
            print_err('--- ERROR ---')
            return

        is_multiple_actions = len(actions) > 1
        for action, args in actions:
            if is_multiple_actions:
                print(f'--- {action.DESC} ---')

            try:
                action.execute(**args)

            except ActionError as ex:  # noqa: PERF203
                error_message = str(ex)
                if error_message:
                    print_err(error_message)

                if is_multiple_actions:
                    print_err(f'--- ERROR ({action.DESC}) ---')
                else:
                    print_err('--- ERROR ---')

                return

            except ActionCancel:
                return

    def _generate_prompt(self) -> str:
        return colored(f'\n{self.active_profile}> ', Color.GREEN)

    def _parse_command(self, user_input: str) -> list[tuple]:
        result = []

        action_re = re.compile(r'\s*([^\s\(\)]+)(?:\((.*?)\))?\s*(?:\s|$)')

        pos = 0
        end_pos = len(user_input)
        while pos < end_pos:
            match = action_re.match(user_input, pos)

            if not match:
                raise CommandParseError('Unable to parse command')

            action_name = match.group(1)
            args_str = match.group(2) or ''

            action = self._get_action_by_shortcut(action_name)
            args = self._parse_arguments(args_str)

            try:
                sig = inspect.signature(action.execute)
                sig.bind(**args)
            except TypeError as ex:
                raise CommandParseError(f'Wrong "{action_name}" action arguments ({ex})')

            result.append((action, args))

            pos = match.end()

        return result

    def _get_action_by_shortcut(self, shortcut: str) -> BaseAction:
        for action in self.actions:
            if shortcut == action.PROMPT_SHORTCUT:
                return action
        raise CommandParseError(f'Unknown action "{shortcut}"')

    def _parse_arguments(self, args_str: str) -> dict:
        args_str = args_str.strip()
        if not args_str:
            return {}

        try:
            wrapper_code = f'stub_function({args_str})'
            tree = ast.parse(wrapper_code, mode='eval')

            if not isinstance(tree.body, ast.Call):
                raise Exception

            # Check for positional args
            if tree.body.args:
                raise Exception

            result = {}
            for keyword in tree.body.keywords:
                # Check for **{...} syntax
                if keyword.arg is None:
                    raise Exception

                if keyword.arg in result:
                    raise Exception

                val_str = ast.unparse(keyword.value)
                result[keyword.arg] = ast.literal_eval(val_str)

            return result

        except Exception:
            raise CommandParseError(f'Failed to parse "{args_str}" expression')


class CliActionManager(BaseActionManager):
    def __init__(self) -> None:
        super().__init__()
        self._init_action_subparsers()

    def _init_action_subparsers(self) -> None:
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()

        for action in self.actions:
            action.cli_add_subparser(subparsers)

        self._options = parser.parse_args()

    def start(self) -> None:
        try:
            self._execute_action()

        except ActionError as ex:
            error_message = str(ex)
            if error_message:
                print_err(error_message)

            sys.exit('--- ERROR ---')

        except ActionCancel:
            sys.exit()

    def _execute_action(self) -> None:
        options = self._options.__dict__.copy()
        del options['func']
        self._options.func(**options)


class ProfileSelectAction(BaseAction):
    PROMPT_SHORTCUT = 'p'
    DESC = 'Profile select'

    def execute(self) -> None:
        variants = {self._decorate_profile(profile): profile for profile in self.manager.profiles}
        selected_profile = Console.select(prompt='Select profile:', variants=variants)

        if selected_profile is None:
            raise ActionCancel

        self.manager.active_profile = selected_profile

    def _decorate_profile(self, profile: str) -> str:
        if profile == self.manager.active_profile:
            return colored(profile, Color.CYAN)
        else:
            return profile


def get_build_env(profile: str) -> Mapping[str, str]:
    env = os.environ.copy()
    if profile.endswith('-static'):
        env['CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_RUSTFLAGS'] = '-C target-feature=+crt-static'
    return env


class BuildAction(BaseAction):
    PROMPT_SHORTCUT = 'b'
    CLI_COMMAND = 'build'
    DESC = 'Build'

    def _cli_add_args(self, subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument('--profile', type=str, required=False)

    def execute(
            self,
            profile: str | None = None,
            ) -> None:
        profile = profile or self.manager.active_profile
        command = f'cargo build --profile {profile} --all-targets'

        print(f'Building ({profile})...')
        Console.run_shell_command(
                command=command,
                cwd=ROOT_DIR,
                env=get_build_env(profile),
                error_exception=ActionError,
                )


class RebuildAction(BaseAction):
    PROMPT_SHORTCUT = 'rb'
    CLI_COMMAND = 'rebuild'
    DESC = 'Rebuild'

    def _cli_add_args(self, subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument('--profile', type=str, required=False)

    def execute(
            self,
            profile: str | None = None,
            ) -> None:
        profile = profile or self.manager.active_profile

        self._clean_profile(profile)
        BuildAction().execute(profile)

    def _clean_profile(self, profile: str) -> None:
        command = f'cargo clean --profile {profile}'

        print(f'Cleaning up ({profile})...')
        Console.run_shell_command(
                command=command,
                cwd=ROOT_DIR,
                error_exception=ActionError,
                )


class RunAction(BaseAction):
    PROMPT_SHORTCUT = 'r'
    CLI_COMMAND = 'run'
    DESC = 'Run executable'
    ARGS = f'{PROMPT_SHORTCUT}, {PROMPT_SHORTCUT}(bin=\'name\'), {PROMPT_SHORTCUT}(example=\'name\')'

    def _cli_add_args(self, subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument('--profile', type=str, required=False)
        subparser.add_argument('--bin', type=str, required=False)
        subparser.add_argument('--example', type=str, required=False)

    def execute(
            self,
            profile: str | None = None,
            bin: str | None = None,
            example: str | None = None,
            ) -> None:
        if bin and example:
            raise ActionError('Only one of the following arguments (bin, example) can be provided')

        profile = profile or self.manager.active_profile
        command = f'cargo run --profile {profile}'
        desc = ''

        if bin:
            command += f' --bin "{bin}"'
            desc = f' binary "{bin}"'
        if example:
            command += f' --example "{example}"'
            desc = f' example "{example}"'

        print(f'Executing{desc} ({profile})...')
        Console.run_shell_command(
                command=command,
                cwd=ROOT_DIR,
                env=get_build_env(profile),
                error_exception=ActionError,
                )


class RunTestsAction(BaseAction):
    PROMPT_SHORTCUT = 't'
    CLI_COMMAND = 'test'
    DESC = 'Run tests'
    ARGS = f'{PROMPT_SHORTCUT}, {PROMPT_SHORTCUT}(test=\'name_pattern\')'

    def _cli_add_args(self, subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument('--test', type=str, required=False)

    def execute(
            self,
            test: str | None = None,
            ) -> None:
        command = 'cargo test'

        if test:
            command += f' {test}'

        print('Executing tests...')
        Console.run_shell_command(
                command=command,
                cwd=ROOT_DIR,
                error_exception=ActionError,
                )


class CleanAction(BaseAction):
    PROMPT_SHORTCUT = 'cl'
    CLI_COMMAND = 'clean'
    DESC = 'Clean generated artifacts (all profiles)'

    def execute(self) -> None:
        command = 'cargo clean'

        print('Cleaning up (all)...')
        Console.run_shell_command(
                command=command,
                cwd=ROOT_DIR,
                error_exception=ActionError,
                )


class DepsDisplayAction(BaseAction):
    PROMPT_SHORTCUT = 'dep'
    DESC = 'Display dependency graph'

    def execute(self) -> None:
        print('Dependency graph:\n')
        command = 'cargo tree'
        Console.run_shell_command(
                command=command,
                cwd=ROOT_DIR,
                error_exception=ActionError,
                )

        print('\nDuplicates:\n')
        command = 'cargo tree --duplicates'
        Console.run_shell_command(
                command=command,
                cwd=ROOT_DIR,
                error_exception=ActionError,
                )


class DepsExportAction(BaseAction):
    PROMPT_SHORTCUT = 'depex'
    DESC = 'Export dependencies'

    def execute(self) -> None:
        DEST_DIR_ENV_VAR = 'CARGO_HOME_EXPORT'

        try:
            dest_dir = os.environ[DEST_DIR_ENV_VAR]
        except KeyError:
            raise ActionError(f'"{DEST_DIR_ENV_VAR}" environment variable not found')

        if not Path(dest_dir).is_dir():
            raise ActionError(f'Destination dir "{dest_dir}" not found')

        command = 'cargo fetch'

        env = os.environ.copy()
        env['CARGO_HOME'] = dest_dir

        print('Exporting dependencies...\n')
        Console.run_shell_command(
                command=command,
                cwd=ROOT_DIR,
                env=env,
                error_exception=ActionError,
                )


class FormatAction(BaseAction):
    PROMPT_SHORTCUT = 'f'
    CLI_COMMAND = 'fmt'
    DESC = 'Format'

    def execute(self) -> None:
        command = 'cargo fmt'

        print('Formatting...')
        Console.run_shell_command(
                command=command,
                cwd=ROOT_DIR,
                error_exception=ActionError,
                )


class OpenExplorerAction(BaseAction):
    PROMPT_SHORTCUT = 'expl'
    DESC = 'Open Windows Explorer'

    def is_active(self) -> bool:
        return IS_WINDOWS

    def execute(self) -> None:
        os.startfile(ROOT_DIR)


class OpenTerminalAction(BaseAction):
    PROMPT_SHORTCUT = 'cmd'
    DESC = 'Open Windows terminal'

    def is_active(self) -> bool:
        return IS_WINDOWS

    def execute(self) -> None:
        subprocess.Popen('cmd.exe /K', cwd=ROOT_DIR, creationflags=subprocess.CREATE_NEW_CONSOLE)


class ClsAction(BaseAction):
    PROMPT_SHORTCUT = 'cls'
    DESC = 'Clear console'

    def execute(self) -> None:
        Console.clear()


class ExitAction(BaseAction):
    PROMPT_SHORTCUT = 'x'
    DESC = 'Exit'

    def execute(self) -> None:
        sys.exit()


class HelpAction(BaseAction):
    PROMPT_SHORTCUT = '?'
    DESC = 'Show action list'

    def execute(self) -> None:
        for action in self.manager.actions:
            action_help = f'  {action.PROMPT_SHORTCUT}\t{action.DESC}'
            if action.ARGS:
                action_help += f': {action.ARGS}'
            print(action_help)


def start_interactive_mode() -> None:
    try:
        InteractiveActionManager().start()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        Console.pause('exit')
        sys.exit(1)


def start_cli_mode() -> None:
    CliActionManager().start()


def get_decorated_root_dir() -> str:
    absolute_path = ROOT_DIR

    if IS_WINDOWS:
        return str(absolute_path)

    home_dir = Path.home().resolve()

    if absolute_path == home_dir:
        return '~'

    try:
        relative_path = absolute_path.relative_to(home_dir)
        return f'~/{relative_path}'
    except ValueError:
        return str(absolute_path)


def update_console_title() -> None:
    console_title = f'Actions ({get_decorated_root_dir()})'
    print(colorama.ansi.set_title(console_title), end='', flush=True)


def main() -> None:
    if IS_WINDOWS:
        colorama.init()

    update_console_title()

    if not sys.argv[1:]:
        start_interactive_mode()
    else:
        start_cli_mode()


if __name__ == '__main__':
    main()
