"""A tiny TCP forwarder used to simulate a broker outage and recovery.

Connections made through it are cut when it stops, and new ones are refused until it starts
again on the same port, like a broker that is restarted.
"""

import asyncio
import contextlib


class TcpProxy:
    def __init__(self, target_host: str, target_port: int) -> None:
        self.target = (target_host, target_port)
        self.port = 0
        self._server: asyncio.AbstractServer | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> "TcpProxy":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        self.port = self._server.sockets[0].getsockname()[1]  # keep the port across restarts
        return self

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()  # no new connections
        # Cut the live ones BEFORE waiting: since Python 3.12 wait_closed() waits for them.
        for writer in list(self._writers):
            writer.close()
        self._writers.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._server.wait_closed(), 2)
        self._server = None

    async def _handle(self, client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
        try:
            upstream_r, upstream_w = await asyncio.open_connection(*self.target)
        except OSError:
            client_w.close()
            return
        self._writers.update({client_w, upstream_w})

        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while data := await src.read(65536):
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, asyncio.CancelledError):
                pass
            finally:
                dst.close()

        await asyncio.gather(pipe(client_r, upstream_w), pipe(upstream_r, client_w))
        with contextlib.suppress(KeyError):
            self._writers.discard(client_w)
            self._writers.discard(upstream_w)
