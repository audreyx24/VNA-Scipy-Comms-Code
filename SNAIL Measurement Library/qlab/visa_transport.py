"""The real pyvisa transport. Lab PC only — pyvisa is not installed offline.

Kept in its own tiny module so that scpi_vna.py never imports pyvisa at module
level, which is what lets the driver be tested on a laptop.

The transport is deliberately dumb: it moves strings and bytes, and knows
nothing about VNAs. All SCPI lives in scpi_vna.py.
"""


class VisaTransport:
    def __init__(self, address, timeout_ms=30000, open_timeout_ms=5000):
        import pyvisa
        self._rm = pyvisa.ResourceManager()
        # open_timeout bounds the claim attempt itself. USBTMC allows exactly
        # one session per device, so if a previous process is still holding it
        # this call blocks down in the driver -- and inst.timeout does not
        # exist yet to govern it. Without open_timeout the very first line of a
        # notebook hangs with nothing to interrupt it.
        self.inst = self._rm.open_resource(address, open_timeout=open_timeout_ms)
        self.inst.timeout = timeout_ms
        self.address = address
        self._closed = False

    @property
    def timeout_ms(self):
        return self.inst.timeout

    @timeout_ms.setter
    def timeout_ms(self, value):
        self.inst.timeout = value

    def write(self, command):
        self.inst.write(command)

    def query(self, command):
        return self.inst.query(command)

    def query_raw(self, command):
        """Send a query and return the raw bytes, header and all.

        We do the IEEE 488.2 block parsing ourselves (parse_binary_block) so
        that the same code path is exercised offline against FakeSCPIInstrument.
        """
        self.inst.write(command)
        return self.inst.read_raw()

    def clear(self):
        """USBTMC Device Clear: abort any transfer in flight, flush both buffers.

        This is the cure for a half-read binary block. A CALC:DATA? SDATA read
        at 2001 points is ~32 kB in one IEEE 488.2 block; if that read is cut
        short (timeout, Ctrl-C, killed kernel) the remainder stays queued in
        the instrument. The next session's first query then reads the *tail of
        the old trace* as its answer, and every response after it is off by
        one for the life of the session.
        """
        self.inst.clear()

    def drain(self, timeout_ms=200, max_reads=64):
        """Read and discard anything still queued, returning the byte count.

        Belt to clear()'s braces: Device Clear should empty the output queue,
        but this proves it rather than assuming it.

        We ask the status byte first and only read when MAV (bit 4) says a
        message is actually waiting. Reading blind would be simpler, but an
        unsolicited read against a *clean* instrument provokes a -420 "Query
        UNTERMINATED" and pushes an error onto the queue we are trying to
        empty -- the diagnostic making the problem it diagnoses.
        """
        import pyvisa
        saved = self.inst.timeout
        self.inst.timeout = timeout_ms
        discarded = 0
        try:
            for _ in range(max_reads):          # bounded: never loop forever
                try:
                    if not (self.inst.read_stb() & 0x10):
                        break                   # MAV clear -> queue is empty
                    chunk = self.inst.read_raw()
                except pyvisa.VisaIOError:
                    break                       # timeout == nothing left
                if not chunk:
                    break
                discarded += len(chunk)
        finally:
            self.inst.timeout = saved
        return discarded

    def close(self):
        """Release the session. Idempotent, and never raises.

        Closing matters more than it looks: the USB claim lives until the
        owning process exits, and Jupyter's "shut down kernel" cannot kill a
        kernel that is blocked inside a native VISA read.
        """
        if self._closed:
            return
        self._closed = True
        for handle in (self.inst, self._rm):
            try:
                handle.close()
            except Exception:
                pass
