"""State persistence — the memory that survives a restart.

Remembers two facts per instrument: the last CLOSED bar handled, and whether the fast EMA
was above/below the slow one on it. These prevent duplicate alerts and make restarts and
overlapping runs safe.
"""
import json
import os
import tempfile

# state.json next to this file. A test run overrides it with SIGNAL_STATE_FILE so a stale
# test state can never fire a phantom signal against the real state.
STATE_FILE = os.environ.get("SIGNAL_STATE_FILE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "state.json"
)


def load_state():
    """Read state.json. Returns {} on first run or if the file is unreadable — a lost memory
    costs at most one alert, whereas crashing would cost all future ones."""
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state):
    """Write state.json ATOMICALLY: temp file -> fsync -> os.replace over the real one, so a
    crash leaves a complete old or new file, never a torn one."""
    directory = os.path.dirname(STATE_FILE) or "."   # guard a bare-filename override
    fd, temp_path = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())   # force bytes to disk before the rename
        os.replace(temp_path, STATE_FILE)
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def get_instrument_state(state, name):
    """One instrument's state with safe defaults: (bar_time, relationship, exchange, source).
    `exchange` is kept because a saved relationship is only valid against the same exchange;
    `source` is kept so we can notify on a source change once, not every cycle."""
    slice_ = state.get(name, {})
    return (
        slice_.get("last_processed_bar_time"),
        slice_.get("last_relationship"),
        slice_.get("last_exchange"),
        slice_.get("last_source"),
    )


def set_instrument_state(state, name, bar_time, rel, exchange=None, source=None):
    """Update one instrument's slice of state (in memory -- caller still saves)."""
    state[name] = {
        "last_processed_bar_time": bar_time,
        "last_relationship": rel,
        "last_exchange": exchange,
        "last_source": source,
    }
    return state
