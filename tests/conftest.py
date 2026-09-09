"""Both the stub app and the service run in-process on the test event loop,
each on its own free port, so a test is a plain HTTP request end to end."""

import asyncio
import socket
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
import pytest
import uvicorn

from playwright_prerender.app import create_app
from playwright_prerender.config import Settings
from playwright_prerender.logs import configure_logging
from .stub_app.app import app as stub_app


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def serve(app, port: int) -> tuple[uvicorn.Server, asyncio.Task[None]]:
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.01)
    return server, task


async def stop(server: uvicorn.Server, task: asyncio.Task[None]) -> None:
    server.should_exit = True
    await task


@pytest.fixture(scope="session")
async def origin() -> AsyncIterator[str]:
    port = free_port()
    server, task = await serve(stub_app, port)
    yield f"http://127.0.0.1:{port}"
    await stop(server, task)


ServiceFactory = Callable[..., Awaitable[str]]


@pytest.fixture(scope="session")
async def service_factory(origin: str) -> AsyncIterator[ServiceFactory]:
    """Start a service with overrides; returns its base URL. All instances
    are torn down at the end of the session."""
    running: list[tuple[uvicorn.Server, asyncio.Task[None]]] = []

    async def start(**overrides) -> str:
        port = free_port()
        settings = Settings(**{"origin": origin, "port": port, **overrides})
        configure_logging(settings)
        server, task = await serve(create_app(settings), port)
        running.append((server, task))
        return f"http://127.0.0.1:{port}"

    yield start
    for server, task in running:
        await stop(server, task)


@pytest.fixture(scope="session")
async def service(service_factory: ServiceFactory) -> str:
    """The default configuration, shared across tests."""
    return await service_factory(blocked_hosts=("blocked.invalid",))


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(follow_redirects=False, timeout=30) as c:
        yield c
