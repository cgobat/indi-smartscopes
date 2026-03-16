
import os
import sys
import fcntl
import asyncio
from pyindi.device import device


class INDIDevice(device):
    """Subclass of pyINDI device class that overrides `.toindiserver()` to prevent blocking IO errors."""

    async def toindiserver(self):
        """Like superclass' `.toindiserver()` but uses `UnblockStdOut` class to prevent `BlockingIOError`s."""

        while self.running:
            output = await self.outq.get()

            with UnblockStdOut():
                self.writer.write(output.decode())
                self.writer.flush()


class UnblockStdOut:
    """Configure stdout for writing without raising `BlockingIOError`.
    
    Copied from https://github.com/scriptorron/indi_pylibcamera, who in turn got it from
    https://stackoverflow.com/questions/67351928/getting-a-blockingioerror-when-printing-or-writting-to-stdout
    """

    def __enter__(self):
        self.fd = sys.stdout.fileno()
        self.orig_flags = fcntl.fcntl(self.fd, fcntl.F_GETFL)
        new_flags = self.orig_flags & ~os.O_NONBLOCK
        fcntl.fcntl(self.fd, fcntl.F_SETFL, new_flags)

    def __exit__(self, *args):
        fcntl.fcntl(self.fd, fcntl.F_SETFL, self.orig_flags)
