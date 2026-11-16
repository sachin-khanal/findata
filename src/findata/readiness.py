"""Constants shared by `start --background` (parent) and the daemon (child). Stdlib only."""

READY_LINE = b"ready\n"  # the one line the daemon writes to the inherited pipe once serving
DEFAULT_WAIT_S = 30.0  # how long the parent waits for it
