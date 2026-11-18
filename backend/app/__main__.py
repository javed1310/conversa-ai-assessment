"""Local entry point that respects the configured API port."""

import sys

import uvicorn

from app.settings import ConfigurationError, load_settings


def main() -> None:
    try:
        settings = load_settings()
    except ConfigurationError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from None
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host="127.0.0.1",
        port=settings.api_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
