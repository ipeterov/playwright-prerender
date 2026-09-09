import argparse

import uvicorn

from .app import app


def main() -> None:
    parser = argparse.ArgumentParser(prog="stub_app")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    uvicorn.run(app, host="0.0.0.0", port=args.port)  # noqa: S104


if __name__ == "__main__":
    main()
