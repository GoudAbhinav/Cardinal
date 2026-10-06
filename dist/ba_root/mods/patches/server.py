import os

from baclassic._servermode import ServerController

from . import patch_method


@patch_method(ServerController, "_execute_shutdown")
def _execute_shutdown(self) -> None:
    """patched method to stop the server from restarting in between a match."""
    if os.getenv("TOURNAMENT_MATCH") is not None:
        return
    return _execute_shutdown.original(self)
