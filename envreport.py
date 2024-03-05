"""
envreport

diffable environment reports
"""

__version__ = "0.0.1.dev"

import argparse
import functools
import json
import logging
import os
import shlex
import subprocess
import sys
import warnings
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import IntEnum
from fnmatch import fnmatch
from pathlib import Path
from shutil import which
from textwrap import indent

log = logging.getLogger("envreport")

try:
    shlex_join = shlex.join
except AttributeError:
    # shlex.join is new in Python 3.8
    import pipes

    def shlex_join(cmd):
        """Backport of shlex.join for Python < 3.8"""
        return " ".join(pipes.quote(arg) for arg in cmd)


@contextmanager
def _prefix_on_path(prefix, force=False):
    """Context manager for inserting a $PREFIX onto $PATH

    Only modifies $PATH runs if $(which python3) is not $PREFIX/bin/python3
    """
    path_before = os.environ["PATH"]
    prefix_bin = prefix / "bin"
    if not force and (which("python3") == str(prefix_bin / "python3")):
        yield
        return
    log.info(f"Adding {prefix_bin} to front of $PATH")
    os.environ["PATH"] = f"{prefix_bin}{os.pathsep}{path_before}"
    try:
        yield
    finally:
        os.environ["PATH"] = path_before


@contextmanager
def nullcontext():
    """Backport contextlib.nullcontext for Python 3.6

    Added in 3.7
    """
    yield


class Level(IntEnum):
    """Level specifies sort-order of outputs"""

    system = 10
    env = 20
    system_packages = 25
    python = 30
    user = 40

    def __lt__(self, other):
        """Compare with integers"""
        if isinstance(other, Level):
            other = other.value
        return self.value < other


class Collector:
    """Base class for a collector

    Subclasses must define:

    - name
    - level
    - detect()
    - collect()

    and should probably define:

    - get_text_report()

    They may also want to define:

    - to_dict()
    - from_dict()
    """

    level: int
    name: str
    path: Path
    details = False  # True to force <details> wrapper, e.g. low-priority info
    plain_text_output = True  # if True, get_text_output is wrapped in a code fence

    def __init__(self, path):
        """Construct collector for path"""
        self.path = Path(path)

    def detect(self):
        """Detect if I'm a relevant data collector

        i.e. return False if I have no information to find

        No need to override if I should always run
        """
        return True

    def _cached_collect(self):
        """Cached caller of .collect()"""
        if "_collect_cache" not in self.__class__.__dict__:
            setattr(self.__class__, "_collect_cache", {})
        cache = self.__class__._collect_cache
        cache_key = (self.path, os.environ["PATH"])
        if cache_key not in cache:
            self.collect()
            cache[cache_key] = self.collected
        else:
            self.collected = cache[cache_key]

    def collect(self):
        """Collect our information

        Only called if self.detect() returns True

        Should populate `self.collected`,
        usually as a dictionary.
        """
        return

    def get_text_report(self):
        """Return my report as diffable text

        Most collectors should override this
        """
        warnings.warn(
            f"{self.__class__.__name__} does not implement get_text_report, using default JSON"
        )
        return json.dumps(self.collected, indent=1, sort_keys=True)

    def to_dict(self):
        """Serialize collection to a dictionary"""
        return {
            "name": self.name,
            "level": self.level,
            "collected": self.collected,
        }

    @classmethod
    def from_dict(cls, path, d):
        """Reconstruct a Collector from a dictionary"""
        self = cls(path)
        self.collected = d["collected"]
        return self


class UnrecognizedCollector(Collector):
    """Collector for from_dict when no matching Collector is found

    Uses basic JSON output rather than nice text report
    """

    @classmethod
    def from_dict(cls, path, d):
        """Construct collector from dict"""
        self = super().from_dict(path, d)
        self.name = d["name"]
        self.level = d["level"]

    def get_text_report(self):
        """Get some text output

        Reports everything as JSON
        since we don't know how to interpret it
        """

        lines = []
        lines.append(f"Unrecognized collector: '{self.name}'")
        lines.append("Don't know how to nicely render collected info:")
        lines.append(json.dumps(self.collected, indent=1, sort_keys=True))
        return "\n".join(lines)


def collect_command_output(cmd, *popen_args, **popen_kwargs):
    """Run a command and collect its output

    Always returns a string, even on failure

    arguments are passed through to Popen
    """

    popen_kwargs["stdout"] = subprocess.PIPE
    popen_kwargs.setdefault("stderr", subprocess.STDOUT)
    cmd_s = shlex_join(cmd)
    log.debug(f"Collecting command output: `{cmd_s}`")
    try:
        with subprocess.Popen(cmd, *popen_args, **popen_kwargs) as p:
            stdout, stderr = p.communicate()
            stdout = stdout.decode("utf8")
            if stderr:
                stdout += "\n" + stderr.decode("utf8")
            return stdout.rstrip("\n")
    except Exception as e:
        log.error(f"Error running {cmd}: {e}")
        return str(e)


class CommandCollector(Collector):
    """Collector base class

    Implements default methods
    to capture and report a simple command output

    Subclasses must define `command`
    (either as class attribute or instance property)
    """

    @property
    def command(self):
        """Subclasses must specify a command attribute or property"""
        raise NotImplementedError("CommandCollector subclasses must specify commands")

    def detect(self):
        """Run this collector if our command can be found"""
        return bool(which(self.command[0]))

    def collect(self):
        """Collect command and its output

        Errors are handled by collect_command_output(),
        """
        self.collected = {
            "command": self.command,
            "output": collect_command_output(self.command),
        }

    def get_text_report(self):
        """Nice representation of terminal output"""
        cmd_string = shlex_join(self.collected["command"])
        output = self.collected["output"]
        return f"$ {cmd_string}\n{output}"


class CondaInfoCollector(CommandCollector):
    """Collect `conda info`"""

    level = Level.python
    name = "conda info"
    command = ["conda", "info"]
    details = True


class CondaListCollector(CommandCollector):
    """
    Collect conda package list with `conda --list`

    Only run if we are in a conda environment
    """

    level = Level.python
    name = "conda list"
    # TODO: normalize output

    command = ["conda", "list"]

    def detect(self):
        """Only run this if we are in a conda environment"""
        if not which("conda"):
            return False
        if os.environ.get("CONDA_PREFIX") == self.path:
            return True
        if (self.path / "conda-meta").exists():
            # our discovered path has conda-meta defined,
            # that probably means a not-fully-activated
            # conda env
            return True
        return False


class SystemReportCollector(Collector):
    """Collect some simple command output for info about the system"""

    level = Level.system
    name = "system-report"
    commands = [
        ["hostname"],
        ["uname", "-a"],
    ]

    def detect(self):
        """Run if any of my commands can be found"""
        for command in self.commands:
            if which(command[0]):
                return True
        return False

    def collect(self):
        """Collect all command outputs"""
        self.collected = {}
        command_outputs = self.collected["commands"] = []
        for command in self.commands:
            if which(command[0]):
                out = collect_command_output(command)
                command_outputs.append(
                    {
                        "command": command,
                        "output": out,
                    }
                )

    def get_text_report(self):
        """Collect each item"""
        lines = []
        for captured in self.collected["commands"]:
            cmd_string = shlex_join(captured["command"])
            output = captured["output"]
            lines.append(f"$ {cmd_string}")
            lines.append(indent(output, "  "))
            lines.append("")
        return "\n".join(lines[:-1])


class AptCollector(CommandCollector):
    """List packages installed with apt"""

    level = Level.system_packages
    name = "apt-get"
    details = True

    # TODO: yum equivalent
    # TODO: need to process or normalize output?
    command = [
        "dpkg-query",
        "--show",
        # "-f",
        # '{"name":"${binary:Package}","version":"${Version}","status":{"want":"${db:Status-Want}","status":"${db:Status-Status}","eflag":"${db:Status-Eflag}"}}\n',
    ]


class WhichCollector(Collector):
    """Resolve paths to common executables with $(which)"""

    level = Level.system
    name = "which"
    plain_text_output = False

    commands = [
        "bash",
        "python",
        "conda",
        "mamba",
        "python3",
    ]

    def collect(self):
        """Collect paths to each command"""
        self.collected = {}
        for command in self.commands:
            self.collected[command] = which(command) or "not found"

    def get_text_report(self):
        """markdown list of each command path"""
        return "\n".join(
            f"- {command}: `{path}`" for command, path in sorted(self.collected.items())
        )


def _squash_paths(text, replacements):
    """Squash paths for more concise cross comparisons

    Squashes e.g. /Users/name/path -> $HOME/path
    """
    for key, value in replacements:
        text = text.replace(value, f"${{{key}}}")
    return text


class LmodCollector(Collector):
    """Collect loaded Lmod modules"""

    level = Level.system
    name = "Lmod modules"
    plain_text_output = False

    def detect(self):
        """Only run if $LOADEDMODULES is defined"""
        return bool(os.environ.get("LOADEDMODULES"))

    def collect(self):
        """Parse $LOADEDMODULES into a list"""
        self.collected = {
            "modules": os.environ["LOADEDMODULES"].split(os.pathsep),
        }

    def get_text_report(self):
        """Report module list as a markdown list"""
        return "\n".join(f"- `{name}`" for name in self.collected["modules"])


class EnvCollector(Collector):
    """Collect environment variables"""

    level = Level.env
    name = "env"

    # TODO: enable user input
    env_patterns = [
        "USER",
        "HOME",
        "SHELL",
        "*PREFIX",
        "VIRTUAL_ENV",
        "*PATH*",
        "*VERSION*",
        "LANG",
        "LC_*",
    ]

    ignored_env_patterns = [
        "__*",
    ]

    def collect(self):
        """Collect any environment variable that matches one of my patterns"""
        self.collected = {}
        for key in sorted(os.environ):
            if any(fnmatch(key, pattern) for pattern in self.env_patterns) and not any(
                fnmatch(key, ignore_pattern)
                for ignore_pattern in self.ignored_env_patterns
            ):
                self.collected[key] = os.environ[key]

    def get_text_report(self):
        """Simple env lines"""
        lines = []
        # split PATH environment variables
        for key, value in sorted(self.collected.items()):
            lines.append(f"{key}={value}")
            # split path-lists for nicer diff viewing
            # leave the long line above for easier copy/paste
            if "PATH" in key and os.pathsep in value:
                for item in value.split(os.pathsep):
                    lines.append(f"#  {item}")
        return "\n".join(lines)


class PythonSiteCollector(CommandCollector):
    """Collect Python site info via `python3 -m site`"""

    name = "python"
    level = Level.python
    command = ["python3", "-m", "site"]


class PipCollector(CommandCollector):
    """Collect pip package list"""

    name = "pip"
    level = Level.python

    # TODO: try parsing/normalizing pip
    # pip list --format json
    command = ["python3", "-m", "pip", "list"]


def _with_prefix(method):
    """Decorator for running a method with $PREFIX/bin first on PATH"""

    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._env_context:
            return method(self, *args, **kwargs)

    return wrapped


class EnvReport:
    """
    An environment report

    To collect a report, generally:

    ```python
    report = EnvReport()
    report.collect()
    report.save("report.md") # or report.json
    ```

    To reconstruct from JSON to re-render reports, use:

    ```python
    report.from_file('report.json')
    """

    envreport_version = __version__

    def __init__(self, path=None):
        """
        Construct report object

        path: Path
        """
        if path is None:
            path = discover_path()
            self._env_context = nullcontext()
        else:
            path = Path(path)
            self._env_context = _prefix_on_path(path)
        self.path = path
        self._discover_collectors()

    def _discover_collectors(self):
        """Discovers collector classes

        Currently only looks in this file,
        but could search for external providers,
        e.g. via entrypoints.
        """
        self._collector_classes = collectors = {}
        for name, obj in globals().items():
            if (
                isinstance(obj, type)
                and issubclass(obj, Collector)
                and hasattr(obj, "level")
                and hasattr(obj, "name")
            ):
                collectors[obj.name] = obj

    @_with_prefix
    def collect(self):
        """Run all collectors"""
        self.collect_date = datetime.now(timezone.utc).isoformat()
        self.collectors = {}
        for collector_class in sorted(
            self._collector_classes.values(), key=lambda cls: (cls.level, cls.name)
        ):
            try:
                collector = collector_class(path=self.path)
                if not collector.detect():
                    log.debug(f"Not collecting {collector.name}")
                    continue
                log.info(f"Collecting {collector.name}")
                collector._cached_collect()
            except Exception:
                log.exception(f"Error in {collector.name} collector")
            self.collectors[collector.name] = collector

    def to_dict(self):
        """Convert env-report to JSONable dict

        Round-trip with .from_dict()
        """
        return {
            "path": str(self.path),
            "collect_date": self.collect_date,
            "envreport_version": self.envreport_version,
            "collectors": {
                name: collector.to_dict() for name, collector in self.collectors.items()
            },
        }

    def save(self, path, *, format=None):
        """save report to path

        format can be 'markdown' or 'json'.
        If format is unspecified,
        guess based on file extension,
        which will be 'json' if extensin is `.json`, otherwise markdown.
        """
        path = Path(path)
        if format is None:
            if path.suffix == ".json":
                format = "json"
            else:
                format = "markdown"

        if format == "json":
            report_text = self.json_report()
        elif format == "markdown":
            report_text = self.text_report()
        else:
            raise ValueError(f"format must be 'json' or 'markdown', not {format!r}")

        with path.open("w") as f:
            f.write(report_text)

    @classmethod
    def from_file(cls, path):
        """Reconstruct an EnvReport from a file"""
        with Path(path).open() as f:
            return cls.from_dict(json.load(f))

    @classmethod
    def from_dict(cls, d):
        """Reconstruct from a dictionary"""
        self = cls(path=d["path"])
        self.envreport_version = d.get("envreport_version", "unknown")
        self.collect_date = d.get("collect_date", "unknown")
        self.collectors = {}
        for name, collector_dict in d["collectors"].items():
            if name in self._collector_classes:
                collector_class = self._collector_classes[name]
            else:
                collector_class = UnrecognizedCollector
            self.collectors[name] = collector_class.from_dict(self.path, collector_dict)
        return self

    def json_report(self):
        """Return a JSON report"""
        return json.dumps(self.to_dict(), indent=1, sort_keys=True)

    def text_report(self):
        """Return a text report"""
        lines = []
        lines.append(f"# env report: {self.path}")
        lines.append("")
        lines.append(f"- collected on: {self.collect_date}")
        lines.append(f"- envreport version: {self.envreport_version}")
        path_replacements = [
            ("PREFIX", str(self.path)),
        ]
        if EnvCollector.name in self.collectors:
            env_collector = self.collectors[EnvCollector.name]
            for name in ("VIRTUAL_ENV", "CONDA_PREFIX", "HOME"):
                if name in env_collector.collected:
                    path_replacements.append((name, env_collector.collected[name]))

        lines.append("")
        lines.append("## paths")
        lines.append("")
        for name, path in path_replacements:
            lines.append(f"- {name}: {path}")
        lines.append("")

        for collector in sorted(
            self.collectors.values(),
            key=lambda collector: (collector.level, collector.name),
        ):
            lines.append(f"## {collector.name}")
            lines.append("")
            text = collector.get_text_report()
            text = _squash_paths(text, path_replacements)
            details = collector.details or (len(text) > 1024)
            if details:
                lines.append("<details>")
                lines.append("")
            if collector.plain_text_output:
                lines.append("```")
            lines.append(text.rstrip())
            if collector.plain_text_output:
                lines.append("```")
            if details:
                lines.append("")
                lines.append("</details>")
            lines.append("")
        return "\n".join(lines)


def discover_path():
    """Discover currently active environment path

    priority:

    1. $VIRTUAL_ENV
    2. $CONDA_PREFIX
    3. python3 -c 'print(sys.prefix)'
    4. interpreter sys.prefix
    """

    for env_name in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        if env_name in os.environ:
            return os.environ[env_name]
    try:
        path_prefix = subprocess.check_output(
            ["python3", "-c", "import sys; sys.stdout.write(sys.prefix)"]
        ).decode("utf8", "replace")
        return Path(path_prefix)
    except Exception as e:
        log.error(f"Failed to get sys.prefix from python3 on $PATH: {e}")
        return Path(sys.prefix)


def main():
    """main entrypoint"""
    parser = _make_arg_parser()
    args = parser.parse_args(sys.argv[1:])

    logging.basicConfig(level=args.log_level, format="[%(levelname)s] %(message)s")
    path = None
    if args.prefix:
        path = args.prefix
    reporter = EnvReport(path)
    reporter.collect()
    if args.format == "markdown":
        report = reporter.text_report()
    elif args.format == "json":
        report = reporter.json_report()
    else:
        raise ValueError(f"Invalid format: {args.format}")
    print(report)


def _make_arg_parser(**kwargs):
    """Construct teh ArgumentParser

    shared by %envreport magic and cli
    """
    parser = argparse.ArgumentParser(description=__doc__, **kwargs)
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_const",
        dest="log_level",
        const=logging.DEBUG,
        default=logging.INFO,
        help="More verbose logging output",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_const",
        dest="log_level",
        const=logging.ERROR,
        help="Less verbose logging output",
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=["markdown", "json"],
        default="markdown",
        help="Format to render output",
    )
    parser.add_argument(
        "prefix",
        nargs="?",
        help="The environment prefix to report on. Default: use $PATH",
    )
    return parser


def _envreport_magic(line):
    """
    Produce and display an environment report

    usage: %envreport [-v] [-q] [-f {markdown,json}] [--plain] [prefix]

    envreport diffable environment reports

    positional arguments:
      prefix                The environment prefix to report on. Default: use
                            $PATH

    options:
      -v, --verbose         More verbose logging output
      -q, --quiet           Less verbose logging output
      -f {markdown,json}, --format {markdown,json}
                            Format to render output
      --plain               Force plain text output (default in terminals)
    """
    import shlex

    from IPython import get_ipython
    from IPython.display import JSON, Markdown, display

    parser = _make_arg_parser(add_help=False)
    parser.prog = "%envreport"

    parser.add_argument(
        "--plain",
        action="store_true",
        help="Force plain text output (default in terminals)",
    )
    # parser.print_help() to produce docstring
    try:
        args = parser.parse_args(shlex.split(line.strip()))
    except SystemExit:
        # suppress SystemExit, e.g. from `-h`
        return
    plain = args.plain
    if not args.plain:
        plain = not getattr(get_ipython(), "kernel", None)
    prefix = args.prefix or sys.prefix
    reporter = EnvReport(prefix)
    reporter.collect()
    if args.format == "markdown":
        report = reporter.text_report()
        if plain:
            print(report)
        else:
            display(Markdown(report))
    elif args.format == "json":
        if plain:
            print(reporter.json_report())
        else:
            display(JSON(reporter.to_dict()))


def load_ipython_extension(ip):
    """Register %envreport magic"""
    log.setLevel(logging.INFO)
    if getattr(ip, "kernel", None):
        # log to stdout in a kernel
        log_stream = sys.stdout
    else:
        # log to stderr in the terminal
        log_stream = sys.stderr
    log.addHandler(logging.StreamHandler(log_stream))
    log.propagate = False

    ip.register_magic_function(_envreport_magic, magic_name="envreport")


if __name__ == "__main__":
    main()
