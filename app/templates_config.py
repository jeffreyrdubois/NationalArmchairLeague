from fastapi.templating import Jinja2Templates
from pathlib import Path
import os

from app.utils import to_eastern

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["enumerate"] = enumerate
templates.env.filters["enumerate"] = enumerate
# Kickoff/lock times are stored as naive UTC but shown to users in Eastern.
# Pipe a datetime through `| eastern` before strftime to render the correct ET
# wall-clock time (e.g. {{ (game.kickoff_time | eastern).strftime('...') }}).
templates.env.filters["eastern"] = to_eastern

# Build identity, shown in the footer of every page. After an Unraid update the
# first question is whether the new image actually took; putting the answer on
# every page means it is never more than a glance away. Set by the publish
# workflow — a hand-built image reports the "-dev" default.
templates.env.globals["app_version"] = os.getenv("APP_VERSION", "0.0.0-dev")
