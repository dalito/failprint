"""Enumeration of possible output captures."""

from __future__ import annotations

import enum
import os
import sys
import tempfile
from contextlib import contextmanager
from io import StringIO, BytesIO, TextIOWrapper
from typing import IO, TYPE_CHECKING, TextIO

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import TracebackType

WINDOWS = sys.platform == "win32"


class Capture(enum.Enum):
    """An enum to store the different possible output types."""

    STDOUT: str = "stdout"
    STDERR: str = "stderr"
    BOTH: str = "both"
    NONE: str = "none"

    def __str__(self):
        return self.value.lower()

    @classmethod
    def cast(cls, value: str | bool | Capture | None) -> Capture:
        """Cast a value to an actual Capture enumeration value.

        Arguments:
            value: The value to cast.

        Returns:
            A Capture enumeration value.
        """
        if value is None:
            return cls.BOTH
        if value is True:
            return cls.BOTH
        if value is False:
            return cls.NONE
        if isinstance(value, cls):
            return value
        # consider it's a string
        # let potential errors bubble up
        return cls(value)

    @contextmanager
    def here(self, stdin: str | None = None) -> Iterator[CaptureManager]:
        """Context manager to capture standard output/error.

        Parameters:
            stdin: Optional input.

        Yields:
            A lazy string with the captured contents.

        Examples:
            >>> def print_things() -> None:
            ...     print("1")
            ...     sys.stderr.write("2\\n")
            ...     os.system("echo 3")
            ...     subprocess.run(["sh", "-c", "echo 4 >&2"])
            >>> with Capture.BOTH.here() as captured:
            ...     print_things()
            ... print(captured)
            1
            2
            3
            4
        """  # noqa: D301
        with CaptureManager(self, stdin=stdin) as captured:
            yield captured


class CaptureManager:
    """Context manager to capture standard output and error at the file descriptor level.

    Usable directly through [`Capture.here`][failprint.capture.Capture.here].

    Examples:
        >>> def print_things() -> None:
        ...     print("1")
        ...     sys.stderr.write("2\\n")
        ...     os.system("echo 3")
        ...     subprocess.run(["sh", "-c", "echo 4 >&2"])
        >>> with CaptureManager(Capture.BOTH) as captured:
        ...     print_things()
        ... print(captured)
        1
        2
        3
        4
    """  # noqa: D301

    def __init__(self, capture: Capture = Capture.BOTH, stdin: str | None = None) -> None:
        """Initialize the context manager.

        Parameters:
            capture: What to capture.
            stdin: Optional input.
        """
        self._temp_file: IO[str] | None = None
        self._capture = capture
        self._devnull: BytesIO | None = None
        self._stdin = stdin
        self._saved_stderr: BytesIO | None = None
        self._saved_stdout: BytesIO | None = None
        self._saved_stdin: TextIO | None = None
        self._stdout_fd: int = -1
        self._stderr_fd: int = -1
        self._saved_stdout_fd: int = -1
        self._saved_stderr_fd: int = -1
        self._output: str | None = None

    def __enter__(self) -> CaptureManager:  # noqa: PYI034 (false-positive)
        if self._capture is Capture.NONE:
            return self

        # Flush library buffers that dup2 knows nothing about.
        sys.stdout.flush()
        sys.stderr.flush()

        # Patch sys.stdin if needed.
        if self._stdin is not None:
            self._saved_stdin = sys.stdin
            sys.stdin = StringIO(self._stdin)

        # Open devnull if needed.
        if self._capture in {Capture.STDOUT, Capture.STDERR}:
            self._devnull = open(os.devnull, mode="wb")
        
        # Create temporary file. 
        # We use a binary file to avoid encoding issues on Windows.
        # Initially we used a pipe but it would hang on writes given enough output.
        self._temp_file = tempfile.TemporaryFile("w+b", prefix="failprint-")
        fdw = self._temp_file.fileno()

        # Save current stdout
        self._saved_stdout = sys.stdout

        # Redirect stdout to temporary file or devnull.
        self._stdout_fd = sys.stdout.fileno()
        self._new_stdout_fd = os.dup(self._stdout_fd)
        if self._capture in {Capture.BOTH, Capture.STDOUT}:
            os.dup2(fdw, self._stdout_fd)
            if WINDOWS:
                # After the os.dup2 call Windows closes the handle automatically.
                # So before writing to stdout, we need to recreate an analogous buffer.
                sys.stdout = _reopen_stdio(sys.stdout, "wb")
        elif self._capture is Capture.STDERR:
            os.dup2(self._devnull.fileno(), self._stdout_fd)  # type: ignore[union-attr]
            if WINDOWS:
                sys.stdout = _reopen_stdio(sys.stdout, "wb")

        # Save current stdout
        self._saved_stderr = sys.stderr
        # Redirect stderr to temporary file or devnull.
        self._stderr_fd = sys.stderr.fileno()
        self._new_stderr_fd = os.dup(self._stderr_fd)
        if self._capture in {Capture.BOTH, Capture.STDERR}:
            os.dup2(fdw, self._stderr_fd)
            if WINDOWS:
                sys.stderr = _reopen_stdio(sys.stderr, "wb")
        elif self._capture is Capture.STDOUT:
            os.dup2(self._devnull.fileno(), self._stderr_fd)  # type: ignore[union-attr]
            if WINDOWS:
                sys.stderr = _reopen_stdio(sys.stderr, "wb")

        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        exc_traceback: TracebackType | None,
    ) -> None:

        if self._capture is Capture.NONE:
            sys.stdout.flush()
            sys.stderr.flush()
            return

        # Restore stdin to its previous value.
        if self._saved_stdin is not None:
            sys.stdin = self._saved_stdin

        # Close devnull if needed.
        if self._devnull is not None:
            self._devnull.close()

        # Flush everything before reading from temp file.
        sys.stdout.flush()
        sys.stdout.close()
        sys.stderr.flush()
        sys.stderr.close()

        # Restore stdout and stderr to their previous values.
        os.dup2(self._new_stdout_fd, self._stdout_fd)
        os.dup2(self._new_stderr_fd, self._stderr_fd)

        if self._saved_stdout is not None:
            sys.stdout = self._saved_stdout
        if self._saved_stderr is not None:
            sys.stderr = self._saved_stderr

        # Read contents from temporary file, close it.
        if self._temp_file is not None:
            self._temp_file.seek(0)
            if WINDOWS:
                # The stdout encoding is not always utf-8 on Windows. The lines 
                # in the temp file may be written with different encoding.
                clean_decoded = []
                for line in self._temp_file.readlines():
                    try:
                        ln = line.decode("utf-8")
                    except UnicodeDecodeError:
                        ln = line.decode(os.device_encoding(0))
                    clean_decoded.append(ln)
                self._output = "".join(clean_decoded)
            else: # Unix, Mac
                self._output = self._temp_file.read().decode("utf-8")
            self._temp_file.close()

    def __str__(self) -> str:
        return self.output

    @property
    def output(self) -> str:
        """Captured output.

        Raises:
            RuntimeError: When accessing captured output before exiting the context manager.
        """
        if self._output is None and self._capture is not Capture.NONE:
            raise RuntimeError("Not finished capturing")
        return self._output  # type: ignore


def _reopen_stdio(f, mode):
    """Reopen standard I/O stream for Windows."""
    if not hasattr(f.buffer, "raw") and mode[0] == "w":
        buffering = 0
    else:
        buffering = -1

    return TextIOWrapper(
        os.fdopen(f.fileno(), mode, buffering),
        f.encoding,
        f.errors,
        f.newlines,
        f.line_buffering,
        write_through=True,
    )


__all__ = ["Capture", "CaptureManager"]
