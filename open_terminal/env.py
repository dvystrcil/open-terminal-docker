import os

from open_terminal import config


def _resolve_file_env(var: str, default: str = "") -> str:
    """Resolve an environment variable with Docker-secrets ``_FILE`` support.

    If ``<var>_FILE`` is set, its value is treated as a path whose contents
    supply the variable's value (trailing whitespace is stripped).  Setting
    *both* ``<var>`` and ``<var>_FILE`` is an error.

    This follows the convention established by the official PostgreSQL Docker
    image (see https://hub.docker.com/_/postgres#docker-secrets).
    """
    value = os.environ.get(var)
    file_path = os.environ.get(f"{var}_FILE")

    if value and file_path:
        raise ValueError(
            f"Both {var} and {var}_FILE are set, but they are mutually exclusive."
        )

    if file_path:
        with open(file_path) as f:
            return f.read().strip()

    return value or default


API_KEY = _resolve_file_env("OPEN_TERMINAL_API_KEY", config.get("api_key", ""))
CORS_ALLOWED_ORIGINS = os.environ.get(
    "OPEN_TERMINAL_CORS_ALLOWED_ORIGINS",
    config.get("cors_allowed_origins", "*"),
)
LOG_DIR = os.environ.get(
    "OPEN_TERMINAL_LOG_DIR",
    config.get(
        "log_dir",
        os.path.join(
            os.environ.get(
                "XDG_STATE_HOME",
                os.path.join(os.path.expanduser("~"), ".local", "state"),
            ),
            "open-terminal",
            "logs",
        ),
    ),
)

# Comma-separated mime type prefixes for binary files that read_file will return
# as raw binary responses (e.g. "image,audio" or "image/png,image/jpeg").
BINARY_FILE_MIME_PREFIXES = [
    p.strip()
    for p in os.environ.get(
        "OPEN_TERMINAL_BINARY_MIME_PREFIXES",
        config.get("binary_mime_prefixes", "image"),
    ).split(",")
    if p.strip()
]

MAX_TERMINAL_SESSIONS = int(
    os.environ.get(
        "OPEN_TERMINAL_MAX_SESSIONS",
        config.get("max_terminal_sessions", "16"),
    )
)
