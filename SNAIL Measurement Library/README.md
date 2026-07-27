# SNAIL Measurement Library

Resonator and SNAIL/qubit measurements for Xu Han's group setup, moved out of
`QLab_tra_HH.ipynb` / `HH_qubit.ipynb` into one small package plus one notebook.
**One measurement = one function call.**

Everything talks **straight to the Keysight PNA N5222B over raw SCPI** — the
instrument is controlled and read directly, and the data comes straight back to
the lab PC:

```
    our code  ->  pyvisa  ->  VNA
```

instead of the five-layer stack the notebooks used:

```
    our code -> QLab_Neon -> sweep/detector -> MC -> KST_VNA -> QCoDeS -> pyvisa -> VNA
```

**There is no PycQED and no QCoDeS anywhere in this package.** You can verify
that in one line: `grep -rn "pycqed\|qcodes" qlab/` returns nothing.

---

## Contents

1. [Quick start](#quick-start)
2. [Files](#files)
3. [Old notebook cell → new function](#old-notebook-cell--new-function)
4. [Where data lands](#where-data-lands)
5. [Deliberate changes & bug fixes](#deliberate-changes--bug-fixes)
6. [What one scan sends to the VNA](#what-one-scan-sends-to-the-vna)
7. [SCPI command reference — what each one physically does](#scpi-command-reference)
8. [Roadmap: the pump measurements & the Anritsu driver](#roadmap)
9. [Learning the N5222B from scratch](#learning-the-n5222b-from-scratch)

---

## Quick start

On the lab PC, with the N5222B connected:

```python
import qlab
st = qlab.connect_scpi()                       # real VNA over SCPI, no PycQED
qlab.resonator_scan(st, center=8.0122e9, span=20e6, power=-30)   # -> CSV + PNG
qlab.all_off(st)                               # RF off, screen back to front panel
qlab.disconnect(st)                            # release the USB claim — do not skip
```

### When the link gets stuck

USBTMC allows **one session per device**, and the claim lives until the owning
process exits. Two failure modes, with different fixes:

Run **`qlab.diagnose()`** first — read-only, never raises, and it tells you which
of these you have.

| Symptom | Cause | Fix |
|---|---|---|
| Screen frozen on an old span | Channel in HOLD — **cosmetic**, link is fine | `qlab.free_run(st)` (now automatic after every scan) |
| Screen barely advances *during* a scan | Point averaging: one sweep dwells `averages`× per point | Nothing — each scan prints its sweep-time estimate first |
| First call hangs or times out on open | Device still **claimed** by a dead process | Task Manager → kill stray `python.exe`; `services.msc` → restart *Keysight IO Libraries Service* |
| Connects, but reads return nonsense | Link is **dirty** — leftover bytes | `qlab.reset_link()` |

`connect_scpi()` is safe to re-run: it closes the previous session first. It did
not always do this, and since USBTMC permits one session per device, re-running
the setup cell while debugging used to stack live sessions onto one endpoint —
which degrades exactly as "it worked fine until I'd run a lot of things".

The dirty-link case is the subtle one. A `CALC:DATA?` read at 2001 points is one
~32 kB binary block; if it is cut short (timeout, `Ctrl-C`, killed kernel) the
remainder stays queued in the instrument, and the *next* session's first query
reads the tail of that old trace as its answer — every response after it is off
by one. It accumulates per aborted run, which is why power-cycling helps a
little less each time.

`connect_scpi()` now sends a USBTMC Device Clear, drains the queue, `*CLS`s, and
verifies `*IDN?` before touching anything instrument-specific, so this should no
longer happen. `reset_link()` does the same against the raw VISA session — below
the driver — for when it does. Also set the PNA's **System → Preferences → Power
On State** to `Preset`; the default `Last State` restores the broken setup on
every reboot.

For scripts, `qlab.session()` is a context manager that always closes:

```python
with qlab.session() as st:                     # closes on Ctrl-C and exceptions
    qlab.resonator_scan(st, center=8.0122e9, span=20e6, power=-30)
```

Don't open one while another `st` is already live — that's a second session to a
single-session device.

Or just open **`measure.ipynb`** and run the cells — it does exactly this, with
the S11 scan, punch-out, stability monitor, and (for reference) the two-tone
calls all laid out.

Prove the hardware link first (once):

```bash
python3 check_control.py      # REACH / TALK / OBEY / MEASURE — watch the screen move
```

To capture a trace you've set up on the **front panel** — or to prove data is
really coming back — read it without reconfiguring anything:

```python
snap = qlab.read_trace(st)    # read-only; saves CSV + PNG, prints diagnostics
```

The CSV includes a `screen_FDATA` column (the instrument's own formatted values)
next to our decode of `SDATA`. If those agree, the data path is confirmed end to
end — and a nonzero peak-to-peak rules out "we're reading a constant".

### Checking against the reference trace

`baseline_vna_snapshot.csv` is a known-good `read_trace` run kept as a
regression baseline (2001 points, 6–7 GHz, trace `CH1_S11_1`, 2026-07-21
11:50:54, `screen_FDATA` agreeing with our decode). Two ways to use it:

```python
qlab.compare_to_baseline()        # replay: no VNA needed, runs on any machine
qlab.compare_to_baseline(snap)    # live: a fresh read_trace vs the reference
```

**Replay** pushes the baseline's own `real`/`imag` back through the same dB and
phase math and checks it reproduces the recorded columns. It is a unit test of
the decode arithmetic — it must agree to floating-point noise, so a failure is
always a code change, never instrument drift. This is the one to run after any
refactor.

**Live** compares a fresh measurement against the reference. That reference is
an *uncalibrated* trace of standing waves in the cabling, so it only reproduces
if the VNA is in the same front-panel state and nothing was recabled; a
mismatch is information, not necessarily a bug. Defaults: 0.5 dB, 5 MHz.

Neither mode proves `read_trace` is *correct* — the baseline came out of this
same code. The claim to correctness rests on `screen_FDATA`, where the
instrument, not us, is the oracle; `compare_to_baseline` re-checks that column
in both modes.

---

## Files

```
qlab/
  __init__.py       the public API (connect_scpi, the measurements, config)
  config.py         addresses, data dir, default params — EDIT HERE, not in code
  scpi_vna.py       the direct SCPI driver for the N5222B  (the core of the rewrite)
  visa_transport.py tiny pyvisa wrapper (kept separate so scpi_vna imports anywhere)
  instruments.py    connect_scpi() / disconnect() / session() / reset_link() / all_off()
  measurements.py   every measurement routine, moved out of the two notebooks
measure.ipynb       the one thin notebook: checks -> resonator scans -> shutdown
check_control.py    "am I really controlling the VNA?" — four-step proof
baseline_vna_snapshot.csv   reference trace for compare_to_baseline()
README.md           this file
```

Eleven files, one dependency (pyvisa). The original two notebooks are frozen
references elsewhere; new work happens here.

---

## Old notebook cell → new function

| Original | New call |
|---|---|
| *(new — no notebook original)* | `read_trace(st)` — snapshot whatever the VNA is showing now |
| *(new — no notebook original)* | `compare_to_baseline([snap])` — check a trace against the reference run |
| QLab_tra cells 10 / 15 / 17, HH_qubit cell 11 | `resonator_scan(st, center, span, ...)` |
| QLab_tra cell 12 (punch-out) | `resonator_power_sweep(st, ...)` |
| QLab_tra cell 14 (2 h monitor) | `stability_monitor(st, ..., n_runs=None)` |
| rf off cleanup cells | `all_off(st)` |

**Not ported yet** — everything that drives the Anritsu pump: HH_qubit cells 9
and 10 (two-tone), 13 and 16–20/30 (pump sweeps), 28 (observe), 32 (Kerr), 33
(power ramp). Those went through PycQED/QCoDeS, which has been removed from this
package entirely; see [Roadmap](#roadmap).

---

## Where data lands

Every measurement's output goes into a per-run folder, mirroring the layout
PycQED itself uses so a whole day of data sits together regardless of which
code path wrote it:

```
<datadir>/
  20260720/                              <- one folder per day  (YYYYMMDD)
    112656_resonator_scan_S21/           <- one folder per run  (HHMMSS_name)
      resonator_scan_S21.csv
      resonator_scan_S21.png
```

`<datadir>` is `config.DATADIR_LAB` (e.g. `C:/Data_HH/stub-s3`), passed once to
`connect_scpi(datadir=...)`. For a new sample, point it at a new base folder and
the dated tree builds underneath. The helper is `config.make_run_dir(datadir, name)`.

---

## Deliberate changes & bug fixes

Everything on real hardware reproduces the notebook call sequence; the changes
below are flagged in the code docstrings.

1. **Stale-sweep read fixed.** The direct scan now always **triggers a fresh
   sweep before reading** (`SCPIVNA.acquire_trace` → `start_sweep_all`). The old
   fake-mode helper read whatever was already in the VNA buffer — fine on an
   empty VNA, wrong on a real resonance.
2. **Dead-trigger fixed (2026-07-21, found on the 116 PC).** `acquire_trace` was
   sending `TRIG:SEQ:SOUR MAN`, which parks the PNA waiting for the front-panel
   Trigger key; `SENS:SWE:MODE SING` only *arms* under MAN, it does not fire.
   So no sweep ran, and the read returned the average buffer `start_sweep_all()`
   had just cleared — every scan came back a mathematically flat −200 dB / 45°,
   identical at all 50 punch-out powers. Now `TRIG:SEQ:SOUR IMM`, and
   `_assert_live_trace()` raises if a returned trace is perfectly constant.
   (Read-only `read_trace` was never affected, which is why the snapshot cell
   showed real data while every scan after it was dead.)
3. **N² averaging fixed.** Every path uses POINT averaging, where one sweep
   already averages each point N times. `start_sweep_all()` now arms a single
   sweep (`SENS:SWE:MODE SING`), not a *group* of N sweeps on top of point
   averaging (which was N×N measurements). Matches the notebook's single trigger.
4. **S-parameter is set explicitly.** `acquire_trace` sends
   `CALC:PAR:MOD:EXT 'S21'`/`'S11'` so `measure='S11'` actually reflects, like
   the notebook did.
5. **Phase is degrees, labeled degrees.** The notebook detector emitted degrees
   but labeled the column `radians`; here the value is the same, the label is
   correct.
6. **`pump_observe` bug fix** — the original used an undefined `power`; it is now
   the explicit `pump_power` argument.
7. **`pump_sweep_fast(settle_s=...)`** — optional settle wait after moving the
   pump before triggering (default `0.0` = original behavior).
8. **`stability_monitor(n_runs=...)`** — the infinite `while True` got an optional
   run limit (`None` = original behavior).
9. **Auto-organized CSV paths** — the hardcoded `C:/…` paths are gone; output
   auto-names into the dated tree above (override with `csv_path=`).

Two things worth confirming once on hardware (no offline test can settle them):

- **Phase looks flat** on a known resonance — proves the electrical delay
  (`CALC:CORR:EDEL:TIME`) still lives in the SDATA the direct read pulls.
- **Real/imag not swapped** — if phase looks mirrored, swap the two slices in
  `get_real_imaginary_data()`.

`SCPIVNA.verify_against_instrument()` checks length, magnitude (against the
screen's own FDATA), and the frequency axis automatically.

---

## What one scan sends to the VNA

`resonator_scan` → `SCPIVNA.acquire_trace()` collapses the notebook's
`measure_resonator_spectroscopy_vna` into this sequence (every command is one
the notebook path also sent):

| Step | SCPI | Why |
|---|---|---|
| select trace + S-param | `CALC:PAR:SEL` , `CALC:PAR:MOD:EXT 'S21'` | which trace, which receiver pair |
| stop free-running | `INIT:CONT OFF` | so we own the trigger, no stale buffer |
| fastest dwell | `SENS:SWE:TIME:AUTO ON` | minimum time per point |
| trigger source | `TRIG:SEQ:SOUR IMM` | `INIT:CONT OFF` above is what holds it idle; this says the arm command itself starts the sweep |
| averaging | `SENS:AVER:COUN n` , `SENS:AVER:STAT ON` , `SENS:AVER:MODE POIN` | average each point N× within one sweep |
| RF on | `OUTP:STAT ON` | actually emit the probe tone |
| sweep window | `SENS:SWE:TYPE LIN` , `SENS:FREQ:STAR/STOP` , `SENS:SWE:POIN` | where and how finely to look |
| settle | `SENS:SWE:DWEL:SDEL 1e-4` | let things settle at sweep start |
| **go** | `SENS:SWE:MODE SING` then `*OPC?` | one fully-averaged sweep, block till done |
| read | `CALC:DATA? SDATA` (binary `REAL,64`) | corrected real/imag pairs, one round trip |

The frequency axis is computed locally with `np.linspace` (it's fully determined
by start/stop/points), so there's no `CALC:X?` round trip.

---

## SCPI command reference

*What actually happens inside the box for every command the code uses. Written
for someone new to VNAs.*

**The source — generating the tone**

| SCPI | Physically |
|---|---|
| `OUTP:STAT ON/OFF` | Master RF switch: connects/disconnects the internal source to the front port. ON = power leaves the port down the fridge line. |
| `SOUR:POW {dBm}` | How hard the source drives. `-30 dBm` (~1 µW) is a weak probe that won't saturate the resonator; `0 dBm` is ~1000× stronger. Sweeping this is the punch-out. |

**Frequency & the sweep — where the source steps**

| SCPI | Physically |
|---|---|
| `SENS:FREQ:STAR/STOP {Hz}` | First and last frequency visited — the window you look through. |
| `SENS:SWE:POIN {n}` | How many discrete frequencies between them. 2001 pts across 20 MHz = one every 10 kHz. |
| `SENS:SWE:TYPE LIN` | Points evenly spaced in Hz (always used here). |

*Single-point trick:* when STAR == STOP and POIN = N, the source **doesn't
sweep** — it parks on one frequency and measures N times. The fast pump sweep
exploits this: the VNA sits on the resonator while N *pump* frequencies step.

**The receiver — the master speed/noise knob**

| SCPI | Physically |
|---|---|
| `SENS:BWID {Hz}` (a.k.a. `SENS:BAND`) | IF-bandwidth: the digital filter width per point. Narrow (500 Hz ≈ 2 ms/pt) = lower noise floor, slower. Wide (10 kHz) = 20× faster, noisier. **The first number to question when optimizing for speed.** |

**Averaging**

| SCPI | Physically |
|---|---|
| `SENS:AVER:STAT ON` / `SENS:AVER:COUN n` | Turn averaging on / how many to average (√n noise drop → 100 avg ≈ 10× less noise at 100× the time). |
| `SENS:AVER:MODE POIN` | Measure each frequency N times *in a row* before stepping. One triggered sweep = the finished averaged result — which is why one `INIT:IMM`/`SING` suffices. **Load-bearing.** |
| `SENS:AVER:CLE` | Empty the averaging accumulator so the next sweep starts fresh. |

**Triggering — what makes a sweep start**

| SCPI | Physically |
|---|---|
| `INIT:CONT OFF` | Take it off free-run: sweeps only when triggered (so a read can't catch a half-finished free-running sweep). |
| `TRIG:SEQ:SOUR IMM` | Where the trigger comes from: the instrument itself. Not the same as free-running — `INIT:CONT OFF` above already stopped that. Use **`MAN`** only if you also send `INIT:IMM`, because `MAN` means "wait for the front-panel Trigger key", and then `SWE:MODE SING` arms a sweep that never fires. |
| `SENS:SWE:MODE SING` | Arm exactly one sweep. (`GRO` + `SWE:GRO:COUN n` arms a group of N — only for SWEEP-mode averaging, **not** POINT.) |
| `*OPC?` | "Operation complete?" — returns 1 and **blocks the PC** until the sweep is truly done. The honest "are you finished". |

**Electrical delay — the one pure-math command**

| SCPI | Physically |
|---|---|
| `CALC:CORR:EDEL:TIME {s}` | Doesn't change the hardware — subtracts the phase slope from the signal's ~64 ns cable travel time, so the residual phase you see is the device's, not the cable's. Flattens the S21 phase. |

**Selecting the trace / S-parameter**

| SCPI | Physically |
|---|---|
| `CALC:PAR:CAT:EXT?` | List the traces defined on the VNA (name + S-param). The driver uses this to *discover* the trace instead of hardcoding a name. |
| `CALC:PAR:SEL '<name>'` | Point this connection's CALC commands at a trace. Required before any `CALC:DATA?` or the read times out. |
| `CALC:PAR:MOD:EXT 'S21'`/`'S11'` | Re-point the trace at a receiver pair. S21 = drive port 1, listen port 2 (transmission). S11 = drive port 1, listen port 1 (reflection). Same source, different listener. |

**Data format & transfer**

| SCPI | Physically |
|---|---|
| `CALC:DATA? SDATA` | The corrected **complex** data: real, imag, … one pair per point, independent of the display. One round trip, non-mutating. What the rewrite reads. |
| `CALC:DATA? FDATA` | The **formatted** data — the single number per point the screen shows. Used only as a cross-check. |
| `FORM:DATA REAL,64` / `ASC` | How numbers cross the wire: 8-byte binary doubles vs text (~15–20 bytes/number, then parsed). Binary is the transfer speedup — values identical. |
| `FORM:BORD NORM` / `SWAP` | Byte order (big/little-endian) for that binary transfer. Must match the decoder or the numbers come out garbage. |

**Housekeeping**

| SCPI | Physically |
|---|---|
| `DISP:WIND:TRAC:Y:AUTO` | Auto-rescale the screen y-axis. Cosmetic — no effect on returned numbers. |
| `SYST:ERR?` | Pop one message off the error queue (`+0,"No error"` or a real complaint). How you find out the instrument silently rejected a command. |

---

## Roadmap

**The pump-based measurements are not in this library.** Two-tone, the pump
sweeps, the Kerr test, and the observe/ramp helpers all drive the Anritsu
MG3692C, and they previously reached it through PycQED/QCoDeS. Rather than leave
that dependency sitting in the package, the whole path was removed — the code
survives in git history and in the two frozen original notebooks.

**To bring them back**, in order:

1. **Connect the Anritsu.** It currently isn't.
2. **Get its programming manual** and confirm its command set. Do *not* assume
   it speaks the same plain SCPI the PNA does — the MG369xC series has its own
   native GPIB command language, and the exact syntax must be checked against
   the manual, not guessed. Then write `scpi_sgen.py` alongside `scpi_vna.py`
   (same shape: dumb transport + a thin command surface). Basic
   frequency/power/on-off is enough to restore `pump_observe`,
   `pump_power_ramp`, and a direct `pump_sweep_fast`.
3. **Then the hardware-triggered fast path** — the real speed win. Its design,
   from the old `KST_VNA_single_freq_Anritsu_list_manual_trigger`: the VNA
   sweeps N points that are all the *same* frequency, and each point emits a
   10 µs pulse from rear-panel **Aux Trig Out 2** into the Anritsu, which sits
   in **list mode** and advances one pump frequency per pulse. One VNA sweep =
   the entire pump scan, hardware-synchronized, **no Python in the loop.**
   This needs the Anritsu's *list-mode* commands specifically — the old
   `set_list_freqs(...)` / `set_to_CW_mode()` calls were never stock QCoDeS and
   were defined nowhere in the project, so their syntax has to come from the
   manual or from whoever wrote the lab-local driver.
4. **Get a saved two-tone dataset** from the old notebook to validate against,
   the way `verify_against_instrument()` validates the VNA path.

Suspected bug worth checking when you get there: the old fast path set
`TRIG:CHAN:AUX:POS` (Aux **1**, which it had just disabled) instead of
`TRIG:CHAN:AUX2:POS` (the one actually pulsing). If two-tone data ever looked
off-by-one in pump frequency, that's why — set `TRIG:CHAN:AUX2:POS AFT`.

None of this blocks the resonator/VNA work, which is already direct.

---

## Learning the N5222B from scratch

A short path if VNAs / SCPI / pyvisa are new:

1. **What a VNA is (concepts).** A VNA sends a tone in and measures what comes
   back vs frequency. S21 = transmitted (port 1 → 2), S11 = reflected. A
   resonator is a dip (S21) or circle (S11). Beginner intro (Keysight
   "Network Analysis Fundamentals").
2. **The instrument's own help.** The N5222B runs Windows and has the entire
   manual + a **Command Finder** built in (press **Help**). It matches *your*
   firmware exactly — the best SCPI reference there is.
3. **pyvisa "hello world":**
   ```python
   import pyvisa
   rm = pyvisa.ResourceManager()
   print(rm.list_resources())                 # confirm the address
   vna = rm.open_resource('USB0::0x2A8D::0x2A01::MY58421887::0::INSTR')
   print(vna.query('*IDN?'))                   # -> Keysight,N5222B,... = success
   ```
4. **SCPI.** Everything after `*IDN?` is sending the right text. The
   [command reference above](#scpi-command-reference) is the ~20 commands this
   library actually uses; cross-check each in the Command Finder for exact
   syntax on your firmware.

**Durable links** (Keysight reshuffles deep links; these move rarely):

| What | Link |
|---|---|
| PNA documentation portal | <https://www.keysight.com/find/pna> |
| N5222B support page | <https://www.keysight.com/us/en/support/N5222B/pna-microwave-network-analyzer-900-hz-10-mhz-26-5-ghz.html> |
| PNA Quick Start Guide (PDF) | <https://www.keysight.com/us/en/assets/9018-05093/quick-start-guides/9018-05093.pdf> |
| Network Analysis Fundamentals (beginner PDF) | <https://nuance.northwestern.edu/documents/2024-04-22-keysight-network-analysis-fundamentals.pdf> |
| pyvisa tutorial | <https://pyvisa.readthedocs.io/en/latest/introduction/communication.html> |
| Triggering the PNA using SCPI | <https://helpfiles.keysight.com/csg/pxivna/Programming/GPIB_Example_Programs/Triggering_the_PNA_using_SCPI.htm> |
