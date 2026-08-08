"""Per-thread retired-instruction counter via perf_event_open.

Client-side driver work is measured in CPU microseconds elsewhere in this
directory, which is correct on a quiet box and useless on a busy one: three
sibling agents build mongod here, and memory-bandwidth contention inflates the
cycles a fixed amount of Python work costs without changing the work itself.
Retired user-space instructions do not move under contention, so this counter
lets a driver change be attributed while the box is loaded, and the CPU-time
figure be taken separately when it is quiet.

Instructions are not a substitute for time -- they say nothing about cache
misses or syscall latency -- so both are reported and neither is converted into
the other.

Usage::

    c = InstrCounter()          # counts this thread only, user space only
    a = c.read()
    ...work...
    b = c.read()
    print(b - a)                # retired user instructions
    c.close()

Returns None from ``open()`` when perf_event_open is unavailable (missing
permission, container without the syscall), so callers can degrade to CPU time
rather than fail.
"""

from __future__ import annotations

import ctypes
import os
import struct

# x86_64
_NR_PERF_EVENT_OPEN = 298

_PERF_TYPE_HARDWARE = 0
_PERF_COUNT_HW_INSTRUCTIONS = 1

_EXCLUDE_KERNEL = 1 << 5
_EXCLUDE_HV = 1 << 6

_FORMAT_TOTAL_TIME_ENABLED = 1 << 0
_FORMAT_TOTAL_TIME_RUNNING = 1 << 1


class _PerfEventAttr(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("size", ctypes.c_uint32),
        ("config", ctypes.c_uint64),
        ("sample_period", ctypes.c_uint64),
        ("sample_type", ctypes.c_uint64),
        ("read_format", ctypes.c_uint64),
        ("flags", ctypes.c_uint64),
        ("wakeup_events", ctypes.c_uint32),
        ("bp_type", ctypes.c_uint32),
        ("config1", ctypes.c_uint64),
        ("config2", ctypes.c_uint64),
        ("branch_sample_type", ctypes.c_uint64),
        ("sample_regs_user", ctypes.c_uint64),
        ("sample_stack_user", ctypes.c_uint32),
        ("clockid", ctypes.c_int32),
        ("sample_regs_intr", ctypes.c_uint64),
        ("aux_watermark", ctypes.c_uint32),
        ("sample_max_stack", ctypes.c_uint16),
        ("__reserved_2", ctypes.c_uint16),
        ("aux_sample_size", ctypes.c_uint32),
        ("__reserved_3", ctypes.c_uint32),
        ("sig_data", ctypes.c_uint64),
        ("config3", ctypes.c_uint64),
    ]


class MultiplexedError(RuntimeError):
    """The kernel time-shared the counter, so its value is an extrapolation."""


class InstrCounter:
    """Retired user-space instructions on the calling thread."""

    def __init__(self) -> None:
        self._fd = -1
        libc = ctypes.CDLL(None, use_errno=True)
        syscall = libc.syscall
        syscall.restype = ctypes.c_int

        attr = _PerfEventAttr()
        ctypes.memset(ctypes.byref(attr), 0, ctypes.sizeof(attr))
        attr.type = _PERF_TYPE_HARDWARE
        attr.size = ctypes.sizeof(attr)
        attr.config = _PERF_COUNT_HW_INSTRUCTIONS
        attr.read_format = _FORMAT_TOTAL_TIME_ENABLED | _FORMAT_TOTAL_TIME_RUNNING
        # Kernel-side instructions belong to the syscall path, which is shared by
        # every arm and is not the thing a driver change moves; excluding them
        # also removes the interrupt noise a loaded box injects.
        attr.flags = _EXCLUDE_KERNEL | _EXCLUDE_HV

        fd = syscall(
            ctypes.c_long(_NR_PERF_EVENT_OPEN),
            ctypes.byref(attr),
            ctypes.c_int(0),  # pid 0: the calling thread
            ctypes.c_int(-1),  # cpu -1: any CPU
            ctypes.c_int(-1),  # no group
            ctypes.c_ulong(0),
        )
        if fd < 0:
            err = ctypes.get_errno()
            raise OSError(err, f"perf_event_open failed: {os.strerror(err)}")
        self._fd = fd

    def read(self) -> int:
        """Instructions retired since the counter was opened.

        Raises MultiplexedError if the kernel had to time-share the PMU, which
        would make the value a scaled estimate rather than a count.
        """
        raw = os.read(self._fd, 24)
        value, enabled, running = struct.unpack("qqq", raw)
        if running != enabled:
            raise MultiplexedError(
                f"counter multiplexed: running {running} of {enabled} ns enabled"
            )
        return value

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> InstrCounter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def open_counter() -> InstrCounter | None:
    """An InstrCounter, or None when perf_event_open is not usable here."""
    try:
        counter = InstrCounter()
    except OSError:
        return None
    try:
        counter.read()
    except (OSError, MultiplexedError):
        counter.close()
        return None
    return counter
