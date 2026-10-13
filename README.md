# Dual CPU miner for Orange Pi 5 (Monero + Verus)

`dualminer.py` is one supervisor that runs **xmrig** (Monero / RandomX) and
**ccminer** (Verus / VerusHash) as child processes, pins each to its own CPU
cores, restarts them if they crash, and lets you switch live between:

| mode     | Monero cores      | Verus cores        |
|----------|-------------------|--------------------|
| `both`   | `cores_dual` (default: big A76, 4-7) | `cores_dual` (default: little A55, 0-3) |
| `monero` | `cores_solo` (default: all) | –        |
| `verus`  | –                 | `cores_solo` (default: all) |
| `off`    | –                 | –                  |

## Setup (on the Orange Pi)

```bash
git clone https://github.com/dwarner5522/dual-cpu-miner-orangepi5.git && cd dual-cpu-miner-orangepi5
./setup.sh              # fetches + builds xmrig, checks ccminer, huge pages, writes dualminer.service
nano config.json        # set monero.wallet and verus.wallet (and pools if you like)
./dualminer.py cores    # confirm which cores were detected as big / little
./dualminer.py          # run
```

## Switching modes

In the foreground terminal, press a key:
`b` both · `m` Monero only · `v` Verus only · `p` pause · `s` status · `q` quit

From another shell (or when running under systemd):

```bash
./dualminer.py mode both      # or: monero | verus | off
./dualminer.py status         # mode, cores, hashrates, SoC temperature
./dualminer.py quit
```

The last mode is remembered across restarts (`run/mode`); `run --mode X` overrides it.

## Core assignment

`cores_solo` / `cores_dual` in `config.json` accept `"big"`, `"little"`, `"all"`,
a range string like `"4-7"` or `"0-3,6"`, or a list like `[4, 5]`.
A miner is only restarted on a mode switch if its core set actually changes, so
giving a coin the same `cores_solo` and `cores_dual` keeps it running untouched
(useful for Monero, which needs a while to rebuild its RandomX dataset).

Which coin deserves the big cores is worth benchmarking on your board: swap the
two `cores_dual` values and compare `./dualminer.py status`.
