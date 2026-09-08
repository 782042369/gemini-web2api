"""Apply the remaining request budget to actual downstream socket writes."""
import socket
import time

from ..budget import current_budget


class BudgetWriter:
    """Delegate file operations while bounding sendall by the active deadline."""

    def __init__(self, writer, connection, terminal_delivery):
        """Wrap a socket writer. Args: underlying writer, socket, terminal-deadline callback. Returns: None."""
        self.writer = writer
        self.connection = connection
        self.terminal_delivery = terminal_delivery

    def __getattr__(self, name):
        """Delegate flush/close/closed etc. Args: attribute name. Returns: underlying attribute."""
        return getattr(self.writer, name)

    def write(self, data):
        """Write within remaining time and restore the socket timeout.

        Args:
            data: HTTP headers, JSON body or SSE bytes.

        Returns:
            Bytes written. A terminal error gets at most one second of best-effort
            delivery even after expiry; ordinary output never renews the budget.
        """
        budget = current_budget()
        terminal = self.terminal_delivery()
        if budget is None and terminal is None:
            return self.writer.write(data)
        timeout = terminal - time.monotonic() if terminal is not None else budget.remaining("response delivery")
        if timeout <= 0:
            raise socket.timeout("terminal response delivery timed out")
        previous = self.connection.gettimeout()
        if previous is not None and previous > 0:
            timeout = min(timeout, previous)
        self.connection.settimeout(timeout)
        try:
            return self.writer.write(data)
        except socket.timeout:
            if budget is not None and terminal is None:
                budget.check("response delivery")
            raise
        finally:
            self.connection.settimeout(previous)
