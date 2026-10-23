# systemd and cgroup fixtures

Real output from the maintainer's box (Debian 12, systemd 252), recorded 2026-10-23 as
`wikimirror-sync`, the user the maintenance job runs as. The job reads Qdrant's unit state and memory
from these sources, and its tests feed it these files.

| File | Command or path |
|---|---|
| `qdrant_show_after_crash.txt` | `systemctl show wiki-mirror-qdrant.service --timestamp=unix -p LoadState,ActiveState,NRestarts,ExecMainStartTimestamp,InvocationID,ControlGroup` (the maintenance job's own command), about seven hours after Qdrant was OOM-killed at 02:12:18 CDT and restarted by systemd 6 s later (`NRestarts=1`). systemd prints the properties in its own order, not the order asked for. Asked separately at the same time, `Result` read `success`: after an automatic restart the result no longer shows the kill, which is why the job counts restarts instead. |
| `missing_unit_show.txt` | The same command for a unit that is not installed: `LoadState=not-found`, `NRestarts=0`, and the timestamp, cgroup and invocation empty. |
| `qdrant_memory.stat` | `/sys/fs/cgroup/system.slice/wiki-mirror-qdrant.service/memory.stat` during the graph rebuild, with the temporary 26G cap: `anon` is the memory Qdrant holds; `file` is page cache, which the kernel reclaims at the cap. |
| `qdrant_memory.max` | `memory.max` beside it: bytes, or the word `max` when the unit has no cap. |
| `list_timers.txt` | `systemctl list-timers 'wiki-mirror*' --all --no-legend --plain`, recorded 2026-10-23 (CDT, as `wiki-mirror status` runs it): the maintenance timer, on the monotonic clock (`OnBootSec`/`OnUnitActiveSec`), for which `NextElapseUSecRealtime` is empty; the update timer; and the build timer, whose LAST and PASSED columns are `-`, the shape of any column that is not set. NEXT is four fields (weekday, date, time, zone), LEFT is one to four words, and the unit is the second-to-last field. |

The cgroup's `memory.events` is not used: its `oom_kill` count starts again at 0 when systemd
restarts the unit, so it read 0 five hours after the kill.
