"""Package data, read relative to the package rather than the working directory."""

import json
from importlib.resources import files


def colours() -> list[str]:
    return json.loads(files(__package__).joinpath("signals.json").read_text())[
        "colours"
    ]
