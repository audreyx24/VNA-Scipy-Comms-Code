"""Central place for addresses, data directories, and default parameters.

Everything that was a magic number scattered across the notebooks lives here.
Edit this file (not the measurement code) when the setup changes.
"""

import os
import time

# ---------------- Instrument addresses ---------------- #
VNA_NAME = 'N5222B3'
VNA_ADDRESS = 'USB0::0x2A8D::0x2A01::MY58421887::0::INSTR'   # Keysight PNA N5222B

SGEN_NAME = 'MG3692C'
SGEN_ADDRESS = 'GPIB0::5::INSTR'                             # Anritsu MG3692C

# QDAC2 currently unused (kept for reference, was commented out in notebooks)
# QDAC_ADDRESS = 'TCPIP0::169.254.0.6::5025::SOCKET'

# ---------------- Data directories ---------------- #
# On the lab PC (Windows). Point this at a new base folder per sample; the
# dated YYYYMMDD/HHMMSS_name tree (see make_run_dir) builds underneath it.
DATADIR_LAB = 'C:/Data_HH/stub-s3'

# ---------------- Common defaults ---------------- #
# Electrical delay of the cabling [s]; flattens the S21 phase.
# Transmission path ~64-67 ns depending on cabling; reflection path ~1 ns.
DEFAULT_DELAY_S21 = 6.5e-8
DEFAULT_DELAY_S11 = 1e-9

DEFAULT_IF_BANDWIDTH = 1000     # [Hz] lower = less noise, slower sweep
DEFAULT_VNA_TIMEOUT = 1000      # [s] read timeout; a 2001-pt/100-avg sweep is ~200 s

# [s] How long to wait for the USB claim when opening. USBTMC is single-session,
# so if a dead process still holds the device this is what turns "the first cell
# hangs forever" into a prompt, readable error. Keep it short.
DEFAULT_OPEN_TIMEOUT_S = 5.0

# ---------------- Reference trace ---------------- #
# A known-good read_trace() run kept as a regression baseline: 2001 points,
# 6-7 GHz, trace CH1_S11_1, taken 2026-07-21 11:50:54. In that run the
# instrument's own formatted trace (screen_FDATA) agreed with our SDATA decode,
# so the file is evidence the data path was correct at the time. Lives next to
# the qlab package; see compare_to_baseline().
BASELINE_TRACE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'baseline_vna_snapshot.csv')


# ---------------- Output-folder organization ---------------- #
def make_run_dir(datadir, name):
    """Create and return a timestamped folder for one measurement's output:

        datadir/YYYYMMDD/HHMMSS_name/

    Everything a single measurement produces (CSV, PNG, ...) goes inside that
    folder, so a whole day's data sits together under one YYYYMMDD folder. The
    date/time convention deliberately matches the one the old PycQED datasets
    used, so new runs sort alongside historical data in the same tree.

    `name` is sanitized for use as a folder name.
    """
    now = time.localtime()
    day = time.strftime('%Y%m%d', now)
    stamp = time.strftime('%H%M%S', now)
    safe = ''.join(c if (c.isalnum() or c in '-_.') else '_' for c in name)
    run_dir = os.path.join(datadir, day, f'{stamp}_{safe}')
    os.makedirs(run_dir, exist_ok=True)
    return run_dir
