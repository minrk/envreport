"""envdiffer

diff environments
"""

__version__ = "0.0.1.dev"


import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from shutil import which

log = logging.getLogger(__name__)


class Level(Enum):
    system = 10
    env = 20
    python = 30
    user = 40

    def __lt__(self, other):
        if isinstance(other, Level):
            other = other.value
        return self.value < other


class Collector:
    # required attributes
    level: Level
    name: str

    def __init__(self, path):
        self.path = path

    def detect(self):
        """Detect if I'm a relevant data collector

        e.g. return False if I have no information
        """
        return False

    def collect(self):
        return

    def get_text_report(self):
        """Return my report as diffable text"""
        return ""

    def to_dict(self):
        """Return a dictionary"""
        return {
            "name": self.name,
            "level": self.level,
            "text_report": self.get_text_report(),
        }


def collect_command_output(cmd, *popen_args, **popen_kwargs):
    """Run a command and collect its output

    Always returns a string, even on failure
    """

    popen_kwargs["stdout"] = subprocess.PIPE
    popen_kwargs.setdefault("stderr", subprocess.STDOUT)
    try:
        with subprocess.Popen(cmd, *popen_args, **popen_kwargs) as p:
            stdout, stderr = p.communicate()
            stdout = stdout.decode("utf8")
            if stderr:
                stdout += "\n" + stderr.decode("utf8")
            return stdout
    except Exception as e:
        log.error(f"Error running {cmd}: {e}")
        return str(e)


class CommandCollector(Collector):
    def get_command(self):
        raise NotImplementedError("override me in subclass")

    def detect(self):
        cmd = self.get_command()
        return bool(which(cmd[0]))

    def collect(self):
        self._output = collect_command_output(self.get_command())

    def get_text_report(self):
        return self._output


class UnameCollector(CommandCollector):
    level = Level.system
    name = "uname"

    def get_command(self):
        return ["uname", "-a"]


class EnvCollector(Collector):
    level = Level.env
    name = "env"

    def detect(self):
        return True

    def collect(self):
        self._collected_env = {}
        for key in sorted(os.environ):
            if "PATH" in key:
                self._collected_env[key] = os.environ[key]

    def get_text_report(self):
        return "\n".join(
            f"{key}={value}" for key, value in sorted(self._collected_env.items())
        )


class PipCollector(CommandCollector):
    name = "pip"
    level = Level.python

    def get_command(self):
        return ["pip", "freeze"]


class EnvReport:
    def __init__(self, path):
        self.path = Path(path)

    def discover_collectors(self):
        collectors = []
        for name, obj in globals().items():
            if (
                isinstance(obj, type)
                and issubclass(obj, Collector)
                and hasattr(obj, "level")
            ):
                collectors.append(obj)
        return collectors

    def collect(self):
        self.collect_date = datetime.now(timezone.utc)
        collector_classes = self.discover_collectors()

        self.collectors = []
        for collector_class in sorted(
            collector_classes, key=lambda cls: (cls.level, cls.name)
        ):
            try:
                collector = collector_class(path=self.path)
                if not collector.detect():
                    continue
                collector.collect()
            except Exception:
                log.exception(f"Error in collector {collector.name}")
            self.collectors.append(collector)

    def to_dict(self):
        return {
            "path": self.path,
            "date": self.collect_date.isoformat(),
            "envdiffer_version": __version__,
            "collectors": [c.to_dict() for c in self.collectors],
        }
        pass

    def json_report(self):
        return json.dumps(self.to_dict())

    @classmethod
    def text_report_from_json(cls, json_report):
        if isinstance(json_report, str):
            json_report = json.loads(json_report)

        lines = []
        lines.append(f"# env report: {json_report['path']}")
        lines.append("")
        lines.append(f"- collected on: {json_report.get('date', 'unknown')}")
        lines.append(
            f"- envdiffer version: {json_report.get('envdiffer_version', 'unknown')}"
        )
        lines.append("")
        for collector in json_report["collectors"]:
            lines.append(f"## {collector['name']}")
            lines.append("")
            lines.append("```")
            lines.append(collector["text_report"])
            lines.append("```")
            lines.append("")
        return "\n".join(lines)

    def text_report(self):
        return self.text_report_from_json(self.to_dict())


def main():
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        path = Path.cwd()
    reporter = EnvReport(path)
    reporter.collect()
    print(reporter.text_report())


if __name__ == "__main__":
    main()
