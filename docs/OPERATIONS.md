# Operations

**Audience:** whoever runs the box. Commands match `deploy/`; if a script and this file disagree, the
script is right, so fix this file. Elsewhere in the docs the mirror's address is written `<mirror-host>`.

**The addresses in this guide are examples.** Substitute your own wherever you see one:

| What | Example used here | Yours |
|---|---|---|
| The LAN (clients, this box, the NAS, the Proxmox host) | `192.168.1.0/24` | `LAN_CIDR` in `site.env` |
| Your router, which is also the LAN's DNS resolver | `192.168.1.1` | the resolver the container will use |
| This box (reserved in your router's DHCP server) | `192.168.1.50` | `<mirror-host>` |
| The embedding server (optional: an Ollama node or Ollama-compatible cluster for the semantic index's embedding run, [The embedding server](#the-embedding-server)) | `192.168.1.60`, port `11434` | `EMBEDDING_HOST`, `EMBEDDING_PORT` in `site.env` |
| The NFS export holding the snapshots (if you use a NAS) | `192.168.1.40:/mnt/tank/wikipedia-mirror` | your NFS export |
| The Proxmox container ID | `<ctid>` | `pct list` shows it |

Paths are inside the container unless a step says **Proxmox host**. `#` is a root prompt, `$` is any shell.

**`wiki-mirror` is the operator command** (`/usr/local/bin/wiki-mirror`, root only). `wiki-mirror status` is the first thing to
run. The shell that `pct enter <ctid>` gives has no `/usr/local/bin` on its PATH, so there `wiki-mirror` is "command not
found": run `exec bash -l` first, or call `/usr/local/bin/wiki-mirror`. Over SSH it is found as usual.

## The box

The reference deployment, with the example addresses above:

| | |
|---|---|
| Container | an LXC, hostname `wikipedia-mirror`, 192.168.1.50 (a DHCP reservation), Debian 12 |
| Size | 6 vCPU, 32 GB RAM (8 GB is the minimum), local NVMe disk |
| Snapshots | NFS dataset `192.168.1.40:/mnt/tank/wikipedia-mirror` (a NAS), mounted on the Proxmox host and bind-mounted at `/mnt/wikipedia` |
| Python | 3.13, built from source by the installer (Debian 12 ships 3.11) |
| Users | `wikimirror` (API), `wikimirror-sync` (download, parse, index), `qdrant`, `root` |
| Services | `wiki-mirror-qdrant`, `wiki-mirror` (API), `wiki-mirror-build` (oneshot), `wiki-mirror-maintain` (oneshot) |
| Timers | `wiki-mirror-build.timer` (monthly), `wiki-mirror-maintain.timer` (every 15 minutes, see [The maintenance job](#the-maintenance-job)), `wiki-mirror-update.timer` (nightly; opt-in, see [The nightly update](#the-nightly-update)) |

### Memory and disk guidance

Memory, with the box at 32 GB:

| Process | Cap | Notes |
|---|---|---|
| Qdrant | `MemoryMax=24G` | The first full keyword build peaked at 5.1 GB of anonymous memory during segment merges and rests at 4.3 GB. The semantic index (W2) keeps its float16 vectors, its HNSW graph and its binary-quantized copy on disk ([D8](DECISIONS.md#d8)); measured after the rebuild, Qdrant's pinned memory rests at 7.4-7.5 GB. The cap is 24G, not that number plus headroom, because under cgroup v2 the page cache of Qdrant's memory-mapped files is charged to its cgroup: a smaller cap means a smaller cache and slower semantic search. On a 32 GB box 24G is what the other units' caps leave (API 1.5G, build 5G, maintenance job 256M, about 1.25 GB for the system); a box with less RAM lowers it by the difference. The pinned peak during a monthly build, when two keyword indexes exist side by side, is an estimate (12 GB) and **still to be measured**. The earlier 20G was sized for the index held in RAM, which needed about 22.7 GB at rest and was killed at 20G on 2026-10-23. A box that set a cap at runtime with `systemctl set-property --runtime` keeps it until reboot; the unit's 24G applies after the next `install.sh` run or reboot. |
| API | `MemoryMax=1500M` | About 0.2 GB at rest. |
| Build | `MemoryMax=5G` | `Nice=10`, idle I/O priority: a build never makes search slow. |
| Embed | `MemoryMax=2G` | Same priorities; holds only a few windows of vectors. |
| Maintenance job | `MemoryMax=256M` | A Python process with the Qdrant client holds about 100 MB, and runs for seconds. |

`systemctl status` shows a `Memory:` line that includes reclaimable page cache. A Qdrant with its semantic index
on disk shows a large figure there on purpose: Qdrant memory-maps those files and the kernel keeps the parts in
use in spare RAM, gives them back under pressure, and a process is never OOM-killed for page cache. The cache is
lent, not owned, so the container's memory graph will read high after a semantic search and that is not a
problem. To judge Qdrant, read anonymous memory instead (`wiki-mirror status` prints it in its Maintenance
section, against the cap):

```bash
# grep '^anon ' /sys/fs/cgroup/system.slice/wiki-mirror-qdrant.service/memory.stat
```

There is deliberately no `MemoryHigh` on Qdrant: it throttled the build into timeouts on 2026-10-17.

Disk, as measured on 2026-10-18 after the move to NVMe: 45 GB used with compression (article store 25 GB,
Qdrant 37 GB logical), 206 GB free. A monthly build needs the new store beside the old one for a few hours, about
100 GB at peak. The build refuses to create a store or collection with fewer than `min_free_disk_gb` (20) GB
free. The snapshot storage holds two snapshots of about 151 GB each. Never put the store or Qdrant on network
storage.

**One figure to plan with** ([SIZING.md](SIZING.md) has the arithmetic): **250 GB of fast local disk** for the
store, the index and the headroom a monthly build needs, plus **about 300 GB** of bulk storage for the two retained
snapshots. On a single disk that is about 550 GB.

`vm.max_map_count` (a kernel setting, changeable only on the Proxmox host) may need raising before the planned
semantic index (W2): `sysctl -w vm.max_map_count=1048576` on the host.

## Install

One-time provisioning, then the installer. Background: [DESIGN.md §11](DESIGN.md#11-w0-what-the-box-and-the-account-turned-out-to-be-2026-10-16).
Before you start: a dedicated x86_64 Debian 12 or 13 container or VM (the installer owns the box's firewall, so not a
shared or Docker host), the sizes in [Memory and disk guidance](#memory-and-disk-guidance), and a free Wikimedia
Enterprise account (step 3). Time: about 30 minutes to install, about 151 GB to download and about 14 hours for the
first build (measured: [The first build](#the-first-build)).

### 1. Get the installer onto the box

The installer is one script that clones the rest. It reads the site file's example from beside itself, so copy both
files first. From a checkout or a download of this repository, on your workstation:

```bash
$ scp deploy/install.sh deploy/site.env.example root@192.168.1.50:/root/
```

A container without root SSH has no `scp` route. On the **Proxmox host**, push the files in instead:

```bash
# pct push <ctid> deploy/install.sh /root/install.sh --perms 0755
# pct push <ctid> deploy/site.env.example /root/site.env.example
```

Or, in a root shell on the box (needs `curl`; read the script before you run it):

```bash
# curl -fsSLO https://raw.githubusercontent.com/SFN-ALPHA/sfn-wiki-mirror/main/deploy/install.sh
# curl -fsSLO https://raw.githubusercontent.com/SFN-ALPHA/sfn-wiki-mirror/main/deploy/site.env.example
# chmod 0755 install.sh
```

### 2. Storage at `/mnt/wikipedia`

The downloaded snapshots (about 300 GB for two) live at `/mnt/wikipedia/snapshots`. The build unit refuses to run
unless `/mnt/wikipedia` is a **mount point** ("is not a mount point"), so a plain directory is not enough. The
article store and Qdrant stay on the container's own local disk, never on network storage.

**Option A: NFS from a NAS (the reference setup).** Any NFS server will do (TrueNAS, a Linux box with `nfsd`, a
commercial NAS). An unprivileged Proxmox container cannot mount NFS, so the host mounts it and binds it in. On the
**Proxmox host**:

```bash
# mkdir -p /mnt/wikipedia-mirror
# echo '192.168.1.40:/mnt/tank/wikipedia-mirror /mnt/wikipedia-mirror nfs defaults,_netdev 0 0' >> /etc/fstab
# mount /mnt/wikipedia-mirror
# pct set <ctid> -mp1 /mnt/wikipedia-mirror,mp=/mnt/wikipedia,backup=0
# pct set <ctid> --nameserver 192.168.1.1 --searchdomain home.example     # --searchdomain is optional
# pct reboot <ctid>
```

`backup=0` keeps a Proxmox backup from including the snapshot data. The nameserver must be on the LAN: the
container's firewall allows DNS to the site's `LAN_CIDR` only ([The site file](#4-the-site-file)), and Proxmox
rewrites `resolv.conf` on every start.

**NFS user mapping.** In the unprivileged container, `wikimirror-sync` writes as UID 100000 plus its own. On the
NFS share, map every user and group to the dataset's owner (on TrueNAS, set **Mapall** user and group; other
servers call it `all_squash` with `anonuid`/`anongid`), and restrict the authorized hosts to the Proxmox host alone.
Map-all, not map-root (TrueNAS's Maproot): Maproot maps only requests that arrive as uid 0,
and none of the container's writes do. The installer creates `/mnt/wikipedia/snapshots` as `wikimirror-sync` and
writes a test file; if that test fails, this mapping is the cause.

**Option B: one local disk, no NAS.** Put the snapshots in a directory on a big local disk and bind-mount that
directory onto `/mnt/wikipedia`, so that it is a mount point. You need about 550 GB in all (250 GB local plus 300 GB
of snapshots).

* On a **VM or a privileged container or bare metal**, as root on the box:

  ```bash
  # mkdir -p /srv/wikipedia /mnt/wikipedia
  # echo '/srv/wikipedia /mnt/wikipedia none bind 0 0' >> /etc/fstab
  # mount /mnt/wikipedia
  # findmnt /mnt/wikipedia           # must print a line
  ```

* In an **unprivileged Proxmox container**, the container cannot mount anything itself, so bind the directory in
  from the **Proxmox host**. Give the directory to the container's root (host uid 100000 in the default mapping), so
  the installer can create `snapshots` inside it as `wikimirror-sync`:

  ```bash
  # mkdir -p /srv/wikipedia
  # chown 100000:100000 /srv/wikipedia
  # pct set <ctid> -mp1 /srv/wikipedia,mp=/mnt/wikipedia,backup=0
  # pct reboot <ctid>
  ```

  Inside the container, `findmnt /mnt/wikipedia` must print a line.

### 3. The Enterprise account and sign-in

Wikimedia Enterprise's **free tier** includes the Snapshot API this mirror downloads from, and needs no credit card.
Create the account at <https://enterprise.wikimedia.com/> (the free sign-up is at
`https://dashboard.enterprise.wikimedia.com/signup/`), verify your email, and note the username and password you chose. The
free tier allows 30 full-snapshot and 1,500 chunk downloads a month ([The first build](#the-first-build) shows how
the service stays within that), and a snapshot refreshes monthly.

The credentials are a username and password in a 0600 file. Type them in a root shell in the container so they never
reach a command line, a log or a repository (the username is lower-cased, as the API requires):

```bash
# install -d -m 0750 /etc/wiki-mirror
# read -r -p 'Enterprise username: ' u; read -r -s -p 'Enterprise password: ' p; echo
# ( umask 077; printf 'WME_USERNAME=%s\nWME_PASSWORD=%s\n' "${u,,}" "$p" > /etc/wiki-mirror/enterprise.env ); unset u p
```

The installer makes it `wikimirror-sync`-owned, 0600. The API user cannot read it. To change the password later, write
the file again the same way. This file exists only on the box.

### 4. The site file

One value differs from every network to the next, and the firewall is built from it, so it is yours to set. It
lives in `/etc/wiki-mirror/site.env` (root:root 0644; no secret belongs in it), never in the repository:

| Key | Required | What it is | Example |
|---|---|---|---|
| `LAN_CIDR` | yes | The IPv4 network your clients and this box share. SSH and the API port are admitted from it, and DNS, NTP and ping go to it. It must be a private network (see below), this box must have an address inside it, and the resolver in `/etc/resolv.conf` must be inside it. | `192.168.1.0/24` |
| `EMBEDDING_HOST` | no | IPv4 address of the embedding server for the semantic index's embedding run: an Ollama node, or the front end of an Ollama-compatible cluster ([The embedding server](#the-embedding-server)). It opens one egress rule: the only LAN host the build user (`wikimirror-sync`) may open a connection to. Leave it out if you will not run the embedding, or if Ollama runs on this box (loopback is already admitted). | `192.168.1.60` |
| `EMBEDDING_PORT` | no | That server's TCP port (Ollama's own is 11434). Give both `EMBEDDING_HOST` and `EMBEDDING_PORT`, or neither; there is no default. | `11434` |

The file is plain `KEY=value` lines, one per key, no quotes and no spaces; blank lines and whole-line `#` comments
are fine (`deploy/site.env.example` is the template). Every key in it ships commented out, `LAN_CIDR` included,
so an unedited copy is refused: remove the `# ` from `LAN_CIDR` and write your own network, even if it happens to be
the example's `192.168.1.0/24`. **Create it before the first run:**

```bash
# install -d -m 0751 /etc/wiki-mirror
# install -m 0644 /root/site.env.example /etc/wiki-mirror/site.env     # copied to /root/ in step 1
# $EDITOR /etc/wiki-mirror/site.env                                   # uncomment and set LAN_CIDR
```

If you skip this, the installer copies the example there itself and stops, telling you to edit it. It stops the
same way while the file is still identical to the example: a firewall built from example values would lock your real
network out, so none is ever applied from them.

**What the installer accepts.** The firewall loads as root, so the file is read as data and never sourced, and each
value must be exactly a valid literal or the installer lists every problem and stops (a dry run lists them as
blockers):

* `LAN_CIDR`: four decimal octets 0-255 without leading zeros, then `/8` to `/32`, written as the network address
  (`192.168.1.0/24`, not `192.168.1.5/24`), and **inside a private block**: `10.0.0.0/8`, `172.16.0.0/12`,
  `192.168.0.0/16`, or the shared address space `100.64.0.0/10` (carrier-grade NAT and Tailscale-style overlays).
  The firewall admits SSH and the API from everything in `LAN_CIDR`, so a prefix shorter than `/8` is refused
  (`/0` would admit the whole internet, `128.0.0.0/1` half of it), and so is any network outside those four blocks:
  a public block as "the LAN" is the same hole at a smaller size.
* `EMBEDDING_HOST` (optional): a dotted-quad IPv4 address, not a name.
* `EMBEDDING_PORT` (optional): digits only, 1 to 65535, no leading zero. Set both embedding keys or neither.
* Only these three keys, each once. A quote, a space, a trailing comment, `export`, `$(...)` or anything else on a
  line is a problem, not something the installer tries to interpret.
* **Who owns the file.** It decides what a firewall applied as root admits, so the installer refuses a file that
  is not owned by root, that is writable by its group or by everyone, or that is a symbolic link, and builds
  nothing from it (`chown root:root /etc/wiki-mirror/site.env && chmod 0644 /etc/wiki-mirror/site.env`, after
  checking what it says). The copy the installer makes of the example is root:root 0644.
* **It will not lock you out.** Beyond the file itself, the installer stops when the SSH session running it comes
  from an IPv4 address outside `LAN_CIDR` (the firewall would let this session finish but refuse the next one from
  there; an IPv6 client or no SSH session is not checked), and when this box has no global IPv4 address inside
  `LAN_CIDR` (`ip -4 -o addr show scope global`; if `ip` cannot be run it warns and skips that check). A dry run
  lists both as blockers.

**Changing it later.** Edit the file and run `/opt/wiki-mirror/deploy/install.sh` again; it renders the firewall
template, installs the result only if it differs from `/etc/nftables.d/wiki-mirror.nft`, checks the whole ruleset
with `nft -c` and applies it. The install is transactional: the file it replaces is kept as
`/etc/nftables.d/wiki-mirror.nft.previous` until that check passes. If the check fails, the previous file is put
back byte for byte (or the new one removed if there was none, together with any include line or
`/etc/nftables.conf` this run added), `nft -f` is never run, and the installer stops, so the next boot never loads
a ruleset that failed. If it cannot undo something it says so in capitals; fix that before rebooting. A
`.previous` file left by an interrupted run stops the next run until you decide which file stands (the message says
how). `install.sh --show-firewall` prints the ruleset the current file produces and changes
nothing (no root needed). `update.sh` renders the same way each night and says so in its log when a commit changes
the ruleset for your site; it never applies it. A `LAN_CIDR` that does not contain the machine you connect from
locks out new connections from it once applied (the session that applies it survives). The installer catches this
for the SSH session it runs in, but not for a different machine you will connect from later, or for a console or
`pct enter` session: if you are locked out, recover from the Proxmox host as in
[Failure modes](#failure-modes-and-recovery), correct the file and run the installer again.

### 5. Run the installer

Run it as root on the box, with the two files from step 1 in `/root/`:

```bash
# /root/install.sh --dry-run        # every step, and anything that would stop the real run
# /root/install.sh                  # about 30 minutes the first time (Python compiles)
# /root/install.sh --auto-update    # the same, and also turn on the nightly self-update (see below)
```

`site.env.example` must sit beside `install.sh` when you run a copy of it (there is no checkout yet to find it in).

By default it clones this repository over anonymous HTTPS (`https://github.com/SFN-ALPHA/sfn-wiki-mirror.git`), so no
account or key is needed. To install from your own fork, pass `--repo <url>`; an SSH URL (`git@github.com:<you>/<fork>.git`)
needs the read-only deploy key described in [Private fork](#private-fork-the-deploy-key) below.

It is idempotent: re-run it after a failure, a restore, a site-file edit or a template change. It checks Debian
12/13, free disk (it warns when it is low; [SIZING.md](SIZING.md) has the figures), the site file, the snapshot
mount and writability and the resolver (inside `LAN_CIDR`), plus the deploy key for an SSH clone; installs packages;
creates the users and directories; builds CPython 3.13 from python.org source (checksum-verified); clones to
`/opt/wiki-mirror`; installs the package into a venv with every dependency at the exact version in
`deploy/constraints.txt`, from wheels only (nothing compiles on the box, nothing newer arrives by
accident); installs Qdrant 1.12.4 (checksum-verified); writes `mirror.yaml`, `qdrant.yaml`, a generated
`WIKI_MIRROR_API_KEY` and an empty `clients.json`; installs and enables the Qdrant unit, the API and the monthly
build timer, and installs the nightly update timer, enabling it only with `--auto-update`; renders the firewall from
the template and the site file and applies it; installs `/usr/local/bin/wiki-mirror`; and proves
Qdrant, the API and the firewall by behaviour (including a fetch as apt's own user).

It never starts a build, never prints a secret, and never overwrites an edited config or an edited site file.
`--adopt-config` replaces `mirror.yaml` or `qdrant.yaml` from the template and keeps a dated backup. Re-running it
never turns an enabled update timer off.

**The nightly self-update is opt-in.** With `--auto-update` (or later `systemctl enable --now
wiki-mirror-update.timer`) the box pulls the `main` branch at 02:20 local time and applies it as root. That means
the box runs whatever lands on the upstream `main` branch (or on your fork's, if you cloned a fork), as root,
unreviewed by you. Without it, nothing changes until you run `wiki-mirror update` yourself. See
[The nightly update](#the-nightly-update).

**Time zone.** If you enable the nightly update, it fires at 02:20 *local* time, so the container's time zone decides
when it runs: a new container is usually on UTC, so set yours once (`timedatectl set-timezone <Region/City>`; plain
`timedatectl` shows the current zone). The monthly build timer is in UTC on purpose and does not follow it.

#### Private fork: the deploy key

Only for an SSH clone URL (for example a private fork); SSH clones are supported from `github.com` only, and
any other host needs an `https://` URL. A URL carrying a user name, token or query string is refused. Create a
read-only GitHub deploy key on that repository,
separate from any other box's (GitHub refuses one key on two repositories, and a separate key can be revoked alone):

```bash
# install -d -m 0700 /root/.ssh
# ssh-keygen -t ed25519 -N '' -C "wiki-mirror-$(hostname)" -f /root/.ssh/wiki_mirror_deploy
# cat /root/.ssh/wiki_mirror_deploy.pub
```

On GitHub open the repository's Settings, Deploy keys, Add deploy key; paste the line; leave **Allow write access**
unchecked. Then run `/root/install.sh --repo git@github.com:<you>/<fork>.git`. The installer stops, before doing
anything long, if the key is missing or not registered, and prints these same steps.

### 6. Give clients their keys

The installer generates one primary key, `WIKI_MIRROR_API_KEY` in `/etc/wiki-mirror/mirror.env`
(root, 0600); it authenticates as the client `primary`. Give it to the primary client, the one application that will
call the API first, or leave it alone and issue every client a key of its own, which is better because the logs then
name the caller and one key can be revoked alone:

```bash
# wiki-mirror key add my-application
```

The key is printed once. See [API.md](API.md#client-keys) and [MCP.md](MCP.md#client-setup) for what clients do
with it. If you must move the primary key to another machine without displaying it, compare fingerprints rather
than values (the two 12-character strings must match):

```bash
# sh -c "grep '^WIKI_MIRROR_API_KEY=' /etc/wiki-mirror/mirror.env | sha256sum | cut -c1-12"
```

Check the service answers (an unauthenticated call should print 401):

```bash
$ curl -s -o /dev/null -w '%{http_code}\n' http://<mirror-host>:8300/v1/wiki/health
```

## The `wiki-mirror` commands

| Command | What it does |
|---|---|
| `wiki-mirror status` | Services, the mirror's health and, when it is `degraded`, each reason as a red line; the semantic index; the maintenance job's last run, Qdrant's restarts and memory, and any collection the job holds; the build unit, the three timers, the checkout's commit, the firewall counters. Exits non-zero if the API does not answer. |
| `wiki-mirror build-now` | Starts the build unit in the background and returns. Resets the retry limit. Refuses if a build is running. |
| `wiki-mirror embed-now` | Starts the semantic index's embedding run ([below](#the-semantic-index-w2-the-first-embedding-run)), or resumes an interrupted one, in the background and returns. Resets the retry limit. Refuses if a run is going. |
| `wiki-mirror logs [api\|build\|embed\|maintain\|qdrant]` | Follows a unit's journal (default `api`). For an idle build, embed or maintenance unit it prints the last 100 lines instead. |
| `wiki-mirror update [--accept-format-change]` | The update ([below](#the-nightly-update)). |
| `wiki-mirror search "<query>" [limit]` | A real query through the real API, default 5 results. |
| `wiki-mirror key add <name>` / `list` / `revoke <name>` | Agent keys ([API.md](API.md#client-keys)). |

The Python entry point behind it is `python -m wiki_mirror <download|parse|index|build|embed|maintain|serve|status|key>`.
`download`, `parse` and `index` run one stage by hand, for resuming one that failed. Exit codes: 0 success, 1 a
stage failed, 2 a configuration or usage error (systemd does not retry 2). `maintain` is the maintenance job,
which the timer runs ([The maintenance job](#the-maintenance-job)).

Settings are in `/etc/wiki-mirror/mirror.yaml` (every key commented at its default; an unknown key
fails the start). The primary key is never in it: the loader refuses an `api_key` there.

### What healthy looks like

`wiki-mirror status` should read: both services `active`; the build unit `idle, last run ok`; mirror `status ok` with
a snapshot date (`empty` before the first build, `building` during one, `degraded` if something is wrong, with each
reason on a red line under the table); a **Semantic index** section that reads `ready` (or `building its search
graph (N of M indexed)` while Qdrant works on it, which is normal after an embedding run) and never `paused` or
`optimizer failed`; a **Maintenance** section whose last run is under 15 minutes old (an hour is the limit), with Qdrant's
restarts after a failure at 0 or long ago, its memory well under its cap, and no problem lines; all three
timers enabled (the update timer only if you chose it); a `last update` row whose outcome is `updated` or
`current`; and **`service_refused` at 0**.
Anything above 0 there means the API or Qdrant user tried to open an outbound connection, which neither ever
should: read the kernel log on the Proxmox host (`journalctl -k | grep wiki-mirror`).

Other checks:

```bash
# wiki-mirror search "Albert Einstein"      # a real query; the article should come first
# nft list counters                   # in_refused / out_refused / service_refused
$ curl -s -o /dev/null -w '%{http_code}\n' http://<mirror-host>:8300/v1/wiki/health   # 401 without a key: up, key required
```

## The first build

```bash
# wiki-mirror status                      # the API is up; the mirror says "empty"
# wiki-mirror build-now                   # starts the build in the background
# wiki-mirror logs build                  # follow it
```

The operator chooses when. The build downloads the whole snapshot (435 chunks, about 150 GB, 236-372 MB each) to
the NAS, parses it into the article store and indexes the passages. Download alone is roughly an hour. The first
build, 2026-10-17 15:45Z to 10-18 05:12Z, took under 14 hours end to end, with indexing at 1,150-1,500
passages/s on CPU alone. It is checkpointed per chunk: stop it, reboot, lose power, and `wiki-mirror build-now`
resumes. The previous snapshot, if any, keeps serving until the new one is complete.

**No restart afterwards.** When the index stage moves the Qdrant alias, the running API notices within 30
seconds, opens the new snapshot's article store read-only, switches to it for new requests and closes the old
store when its last request ends. If the new store cannot be opened, the API keeps serving the old snapshot and
`wiki-mirror status` says `degraded`; fixing the cause is enough, it retries every 30 seconds.

### What a build refuses, and what it leaves behind

* **One at a time.** A lock (`state/build.lock`, released when the process ends, even by `kill -9`) means a
  second `build`, `parse`, `index` or `download` exits 1 with "a build is already running" and changes nothing.
* **Not enough disk.** Before creating a store or collection it checks free space against `min_free_disk_gb`
  (20; `0` turns the check off).
* **A damaged chunk** (truncated or corrupt `.tar.gz`) fails the parse; the build deletes that file and its
  `.etag`, so the next `wiki-mirror build-now` downloads it again.
* **A finished store is never reopened for writing.** If Enterprise now lists different chunks for a date whose
  store is complete, the build fails rather than mix two versions.
* **Cleanup after a swap.** Article stores and index states older than the serving snapshot and the one kept as
  the way back are deleted, and so are superseded Qdrant collections. Newer files, the resumable work of an
  unfinished build, are left alone.
* **The chunker.** Each index records the settings that cut its passages. If the code's change later, the API
  logs a warning and `wiki-mirror status` reads `degraded` until the index is rebuilt. A build never resumes an index
  cut by another chunker, and it refuses to touch the collection that is serving without a usable index state
  ("delete the collection by hand to rebuild it").
* **Passages carry a text hash.** A search drops a hit whose hash differs from the stored article's and logs the
  page id. In normal running that never happens; a burst of such lines means the index and the store are not
  from the same text (for example a store re-parsed under a newer parser while the old index serves): rebuild
  the index.
* **The parser version.** A parse resumed by code with a different parser version fails. Delete the unfinished
  store (`articles/<identifier>_<date>.db`) and run `wiki-mirror build-now`.
* **Qdrant segments are capped** (`max_segment_size` 400,000 KB, one optimization at a time) so a merge fits
  memory. A collection made before that can be changed live:
  `curl -X PATCH http://127.0.0.1:6333/collections/<name> -H 'Content-Type: application/json' -d '{"optimizers_config": {"max_segment_size": 400000, "max_optimization_threads": 1}}'`.

**A failed build is retried, then it stops.** The unit is `Restart=on-failure` with `RestartSec=1h` and a limit of
six starts in a day: the first run and five hourly retries, each resuming from its checkpoints. After that it
stays `failed` until `wiki-mirror build-now`. Exit 2 is never retried. Every failure is also written to the build state
with a short reason (no secrets), so `wiki-mirror status` and `/v1/wiki/health` show `degraded` with it.

**Download budget.** The free tier allows 30 full-snapshot and 1,500 chunk downloads a month. A full pull is 435
chunks. The service refuses to start a download past `monthly_chunk_budget` (1,200), so a failure loop cannot
spend the month; two full pulls fit with room for retries. If the budget is spent, the build says so and waits
for the 1st.

## The semantic index (W2): the first embedding run

The keyword index answers search alone until the semantic index exists. `wiki-mirror embed-now` embeds every passage
through an Ollama-compatible embedding server into a second Qdrant collection, `wikipedia_semantic_<born>`. It takes
about two days for the 41.6M passages. Nothing reads the new collection yet (hybrid search is later work), so the
API and the keyword index are untouched while it runs. It needs a finished keyword build first (it embeds the store
that build serves), and the run stops with "there is no lexical collection to embed from" if there is none.

### The embedding server

The mirror speaks Ollama's embedding API and nothing else: `POST <embedding_url>/api/embed` with
`{"model": "bge-m3", "input": [...]}`, answered by `{"embeddings": [...]}`. Two setups are supported: one Ollama
node, or an Ollama-compatible cluster behind one URL. Either way the work runs on your own hardware.

**One Ollama node.** A machine with a GPU on the LAN, or this box itself if it has a GPU passed through.

1. On the node, fetch the model: `ollama pull bge-m3`.
2. Ollama listens only on `127.0.0.1` by default. For the mirror to reach a node on the LAN, run
   `systemctl edit ollama` on the node, add the lines below, then `systemctl restart ollama`:
   ```ini
   [Service]
   Environment="OLLAMA_HOST=0.0.0.0:11434"
   Environment="OLLAMA_NUM_PARALLEL=4"
   ```
   `OLLAMA_NUM_PARALLEL` is how many requests the model serves at once; the rest wait in Ollama's queue. Ollama has no
   authentication, so anyone who can reach the port can use it: limit port 11434 to the mirror's address in the
   node's own firewall.
3. On the mirror:
   * In `site.env`, set `EMBEDDING_HOST=<node IPv4>` and `EMBEDDING_PORT=11434`, then apply with `install.sh`.
   * In `mirror.yaml`, set `embedding_url: http://<node IPv4>:11434` and `embedding_concurrency` a little above
     `OLLAMA_NUM_PARALLEL` (6 for 4). More only waits in Ollama's queue; fewer leaves the GPU idle between replies.
   * No key file is needed.

   **For Ollama on this box**, use `embedding_url: http://127.0.0.1:11434` and leave the embedding pair out of
   `site.env`: the firewall already admits loopback. Do this only with a GPU. On the CPU, 41.6M passages take weeks.

Ollama unloads a model after five idle minutes (`OLLAMA_KEEP_ALIVE`). A continuous run keeps it loaded. After a
pause, the first requests wait while it loads again, which the run's retries absorb.

**An Ollama-compatible cluster.** A router or load balancer in front of several Ollama nodes, presenting one URL. The
mirror sends everything to that one URL and does not spread requests across nodes itself, so a cluster needs a single
front end. What the cluster must do:

* **Answer a batched request with one vector per input, in order.** Some routers have returned only the first vector
  of a batch. The run stops rather than pad, and the check in step 1 below catches it before you start.
* **Run the same bge-m3 build on every node**, so a passage gets the same vector whichever node embeds it. The
  canary in [EVALUATION.md](EVALUATION.md) embeds one text many times and compares the results; run it again after
  any node upgrade.
* **Say "busy" with 429 or 503**, ideally with `Retry-After` in seconds. The run waits and retries each request for up
  to 15 minutes. 500, 502 and 504, timeouts and dropped connections are retried the same way. 400, 401, 403 and 404
  stop the run at once.

Settings for a cluster:

* `EMBEDDING_HOST` and `EMBEDDING_PORT` are the router's.
* If the router authenticates its clients, issue one key for the mirror alone and write it to the key file (step 2
  below).
* If the router has a background-priority header, set it in `embedding_headers`, so the bulk run yields to
  interactive traffic: for example `embedding_headers: {X-Priority: "3"}`.
* Start at the defaults: 16 requests in flight and 8 texts per request. They were measured on a four-node router,
  where more requests only queued and larger batches did not help ([EVALUATION.md](EVALUATION.md)). A smaller
  cluster wants fewer requests in flight, about four per node.

**Either way:**

* Write `embedding_url` with the IPv4 address you gave as `EMBEDDING_HOST`. The firewall admits that one address; a
  hostname works only if it resolves to it.
* The model is bge-m3 at 1,024 dimensions, the one the evaluation measured. `embedding_model` and
  `embedding_dimensions` can name another, but its quality is unmeasured, and it needs a new collection: a resume
  refuses a model change.

**Not supported:** servers that speak only the OpenAI-style `/v1/embeddings` API (vLLM, Text Embeddings Inference,
LiteLLM and the like), and hosted embedding services.

### Before the first run

1. **Check the server answers one vector per text.** A server that returns fewer vectors than texts stops the run
   (by design, never padded), so try a two-text request first:
   `curl -s -H "Authorization: Bearer $EMBEDDING_API_KEY" -d '{"model":"bge-m3","input":["a","b"]}' <url>/api/embed`
   should show two vectors of 1024 numbers.
2. **Write the key file, if the server wants a key.** In a root shell, so the key never reaches a command line or a
   log (a server without authentication needs no file):
   ```bash
   # read -r -s -p 'Embedding key: ' k; echo
   # ( umask 077; printf 'EMBEDDING_API_KEY=%s\n' "$k" > /etc/wiki-mirror/embedding.env ); unset k
   # chown wikimirror-sync:wikimirror /etc/wiki-mirror/embedding.env; chmod 0600 /etc/wiki-mirror/embedding.env
   ```
   Only the embed unit loads this file. The key is sent as a bearer header and never appears in a setting, a log or
   the state file.
3. **Set `embedding_url`** in `/etc/wiki-mirror/mirror.yaml` (the server's origin, e.g. `http://192.168.1.60:11434`;
   the firewall must admit `wikimirror-sync` to it: `EMBEDDING_HOST` and `EMBEDDING_PORT` in `site.env`, applied
   with `install.sh`; per setup, see [The embedding server](#the-embedding-server)). The other `embedding_*` keys have
   the measured cluster defaults (16 requests in flight, 8 texts per request; lower the first for one Ollama node);
   `embedding_headers` can carry a background-priority header such as `X-Priority: 3` if the server has
   one. Without `embedding_url` the command exits 2.
4. **Give the container 32 GB of RAM** (`pct set <ctid> -memory 32768` on the host, live for an LXC; 8 GB is the
   minimum for the keyword index alone). The semantic index keeps its vectors, its HNSW graph and its binary-quantized
   copy on disk ([D8](DECISIONS.md#d8)), so beside the keyword index's 4.3 GB Qdrant pins only about 2.7 GB of
   per-point bookkeeping (7.4-7.5 GB at rest, measured after the first rebuild); the files in use are
   cached in the RAM that is left, which is why 32 GB is still the size to give. Disk needs about 100 GB more than
   today (the float16 vectors about 85 GB, the binary copy 5.3 GB, the graph about 10 GB, plus payload); the run refuses to create the collection below `min_free_disk_gb`.
   Qdrant's cap in the unit (`MemoryMax`) is 24G, which on a 32 GB box leaves the other units their caps and Qdrant
   its cache ([the memory table](#memory-and-disk-guidance)); `wiki-mirror update` installs the unit as it is.

Then:

```bash
# wiki-mirror embed-now          # starts the unit in the background and returns
# wiki-mirror logs embed         # follow it: a progress line each minute (articles, passages/s, time left)
```

What it reads and writes:

* **It reads** the article store that the live keyword collection serves, and cuts the same passages the keyword
  build cut.
* **It writes** the collection `wikipedia_semantic_<born>` and its state file `state/index_semantic_<born>.json`
  (`<born>` is the date tag of that store's snapshot). When every passage is in, it switches HNSW on and moves the
  alias `wikipedia_semantic` to the collection, with the graph and the binary copy on disk. Qdrant then builds the
  graphs in the background, on disk, `SEMANTIC_OPTIMIZATION_THREADS` (2) segments at a time, which takes hours
  after the run: on the maintainer's box about 4 h 38 min had built 62% of the graph before the 2026-10-23 incident
  ([D8](DECISIONS.md#d8)), at about 93,000 passages a minute. Watch it in `wiki-mirror status` (Semantic index:
  `building its search graph (N of M indexed)` until `ready`), or `curl -s localhost:6333/collections/wikipedia_semantic`
  until its `status` is `green`. The build is Qdrant's own and survives a restart only with help: a restart in the
  middle leaves the collection `grey` (paused) and the maintenance job resumes it within 15 minutes ([The
  maintenance job](#the-maintenance-job)). Search does not use the collection yet.
* **It holds the build lock for the whole run**, so the monthly build waits (exits 1, "a build is already running")
  until the embedding is done, and `wiki-mirror update` defers the same way.
* **A failure does not turn health `degraded`:** the embed stage never writes the build state.

### Interruptions and resuming

The run is built to be stopped. Articles are embedded in page-id order, in windows of 64. A window is upserted into
Qdrant, and only when Qdrant has confirmed it is the checkpoint (`last_page_id` in the state file) moved past it. The
state file is written atomically (a temporary file, then a rename), so a crash never leaves a torn one. So the rule
is: **every article at or below `last_page_id` is fully in Qdrant.** Points beyond it may exist, when a crash fell
between an upsert and the state write; they are harmless, because a passage's point id is fixed by its article and
passage number, and the resume writes them again.

A resume checks two things before it embeds anything:

1. **The settings.** The state records the chunker, model, dimensions and text form. If any differs from the running
   configuration the run stops and says which (mixing two models' vectors in one collection ruins it silently).
2. **The contents.** It asks Qdrant for the exact number of points with a page id at or below the checkpoint and
   compares it with the passage count in the state. If they match, the log says `checkpoint verified` and the run
   goes on. If they do not, Qdrant has lost some of what the checkpoint promised, which is what a power loss or a
   host crash can do to the tail of its write-ahead log while the state file survives. The log then warns
   (`the checkpoint at page id N ... does not match Qdrant`), the checkpoint steps back to just before the highest
   article still there, the counts are taken again, the corrected state is written, and the run embeds from that
   point. The highest surviving article is embedded again in full because it may be only partly written. Qdrant loses
   a tail, not the middle, so nothing below that article is missing. With no points left at all the run starts
   over from the first article. The check is one exact count over the points at or below the checkpoint, about half a
   minute on 41 million points (so a resume is quiet for that long before its first line); it runs only on a resume,
   never for a new run or a finished one. A
   finished run (`complete` in the state) is left alone and rescans nothing.

The state file and the collection must come as a pair. A collection with no usable state is refused and never
deleted for you: two days of vectors are not discarded on a guess. A state file whose collection is gone starts the
run over, with a warning. The closing step, HNSW on and the alias, is idempotent, so a run that died in it repeats it.

What happens when something goes wrong:

| Event | What happens |
|---|---|
| Crash, server error, Qdrant restart | The unit waits 15 minutes and starts again, which resumes. Up to six starts in a day (the first and five retries); after that the unit stays `failed` until `wiki-mirror embed-now`. |
| Embedding server slow or down | Each request is retried for up to 15 minutes (backoff, and `Retry-After` is honoured), then the run fails and the unit's retry takes over. |
| A refusal (400, 401, 403, 404) or a reply of the wrong shape | The run stops at once with a fixed message that says what to check, never the key or any text. Fewer vectors than texts, a wrong-sized vector or a non-number are all refused; nothing is padded. Fix the cause, then `wiki-mirror embed-now`. |
| `systemctl stop wiki-mirror-embed` | The process is stopped where it is and the unit does not retry (a stop is not a failure). Windows in flight are repeated. `wiki-mirror embed-now` resumes. |
| Reboot of the box or container | It does **not** restart by itself: the unit is not enabled and has no timer. Run `wiki-mirror embed-now`. |
| Power loss | The resume verification catches a lost tail and steps back (above). |
| Deploying new code mid-run | The updater defers while the build lock is held. Stop the unit (`systemctl stop wiki-mirror-embed`), run `wiki-mirror update`, then `wiki-mirror embed-now`. If the new code cuts passages differently or changes the text form, the resume is refused (below). |

An interruption costs at most the windows that were in flight, a couple of minutes of work, plus, after a power
loss, whatever Qdrant lost. To start over on purpose, delete the collection (`curl -X DELETE
http://127.0.0.1:6333/collections/wikipedia_semantic_<born>`) and its state file together.

### What is not built yet

* **No catch-up after a monthly rebuild.** Running `embed` after the keyword index was rebuilt from a newer snapshot
  starts a new full collection, `wikipedia_semantic_<new born>`: about 85 GB and about two days. Embedding only
  the articles whose text changed is not built (it is the semantic half of the in-place update, W3, in
  [ROADMAP.md](ROADMAP.md)). Until then, run `embed-now` only when you want that cost, and delete the superseded
  semantic collection and its state file by hand when you no longer need it.
* **No hybrid search.** The collection is built and the alias points at it, but no query reads it.
* **Semantic search latency depends on the page cache.** Measured after the rebuild (over loopback, `limit` 10): about
  45 ms once cached, about half a second the first time a query touches the files, and seconds to tens of seconds
  right after a restart or with a cold cache. A first-time query reads about 290 MB from disk. The levers if that is
  too slow for hybrid search, none done yet, are listed in [D8](DECISIONS.md#d8).

## The maintenance job

`wiki-mirror-maintain.timer` runs `python -m wiki_mirror maintain` (through the oneshot
`wiki-mirror-maintain.service`) 5 minutes after boot and then every 15 minutes, as `wikimirror-sync`. It exists
because of the incident of 2026-10-23, when Qdrant was killed for memory, systemd restarted it six seconds later, and
the semantic collection then sat paused with nobody told ([D8](DECISIONS.md#d8)). The service has `MemoryMax=256M`,
no `Restart=` (the timer is the retry), and is ordered after Qdrant but does not require it, so a run still happens
and records the fact when Qdrant is down. It is safe on every box and takes seconds.

### What each run does

1. Reads Qdrant's systemd unit (`systemctl show`: whether it is installed and running, how
   many times systemd restarted it after a failure, when the running process started) and its memory (`anon` in the
   unit's `memory.stat`, and `memory.max`, its cap).
2. Lists the mirror's collections in Qdrant (`wikipedia_lexical_*`, `wikipedia_semantic_*`).
3. **Resumes** a collection whose status is `grey`, unless it is held ([below](#the-crash-loop-guard)). Qdrant 1.12
   does not restart pending optimizations after it restarts until some update operation arrives; the job sends the
   empty update that starts them (`PATCH /collections/<name>` with `{"optimizers_config": {}}`).
4. **Reconciles** the semantic collection behind the `wikipedia_semantic` alias with the code: the graph's `m`,
   `ef_construct` and `on_disk`, the binary copy's `always_ram`, and the optimizer thread count must equal the
   constants `SEMANTIC_HNSW`, `SEMANTIC_QUANTIZATION` and `SEMANTIC_OPTIMIZATION_THREADS` in
   `src/wiki_mirror/embed.py`. Only the parts that differ are sent, in one update.
5. Writes the record, `state/maintenance.json`, which `/v1/wiki/health` and `wiki-mirror status` show
   ([API.md](API.md#get-v1wikihealth)).

It never starts or retries an embedding run, a build or an update, and it leaves the keyword collection's settings
and all segment sizes alone. Nothing pushes a notification; health and `wiki-mirror status` are where a problem shows.

On a box whose semantic collection already has the settings the code names (the maintainer's, whose placement was
changed by hand on 2026-10-23), the first run finds them equal and does nothing.

### The crash-loop guard

Qdrant leaves a collection `grey` after a restart on purpose, so that an optimization that just ran it out of memory
does not start again by itself and kill it again. Resuming every `grey` collection on every run would turn one such
optimization into a kill every 15 minutes, and every kill takes keyword search down. So the job resumes carefully.

The guard keeps no state of its own. It reads systemd's `NRestarts`: the number of times systemd restarted Qdrant
after a failure since Qdrant was last started by hand. That number survives everything that can happen to the job (a
lost or damaged record, a run killed by its timeout, an update that timed out but was applied), which a record the job
keeps of its own work would not.

A `grey` collection is resumed by itself only while `NRestarts` is **at most 1**. Otherwise it is **held**: neither
resumed nor given a settings update. Its `held` field says why, a problem sentence says it too, and health turns
`degraded`. The reasons, checked in this order:

1. The restart count cannot be read: Qdrant's unit could not be read, it is not installed, or systemd gave no count.
   `held` ends with the command to resume by hand. The hold lifts when the count can be read.
2. The count is 2 or more. `held` ends with the command to resume by hand. Fix the cause (the memory cap, the disk),
   then resume it with that command, which `wiki-mirror status` shows, or start Qdrant by hand, which resets the
   count.
3. Qdrant's anonymous memory is above 90% of its cap. The 90% is a judgment, the same line health uses for "close to
   being killed", not a measured value.
4. Free disk is under `min_free_disk_gb`, or the disk could not be checked.

Holds 3 and 4 are checked on every run and lift by themselves when the condition clears.

The bound: at most two crashes follow a start by hand before the job stops. A first crash the job did not cause is
resumed once; if that resume kills Qdrant, the count is 2 and the collection is held. After a clean start (a reboot, a
start by hand: count 0), a resume that kills Qdrant is retried once more, then held.

A start by hand, `systemctl reset-failed` and a reboot reset the count and give the job its allowance again. That is
deliberate: the operator has looked. The accepted cost is that a second crash holds the collection even when the job
did not cause it; two crashes since the last start by hand are worth a person's look. A box whose Qdrant unit already
shows `NRestarts` of 2 or more (check with `systemctl show wiki-mirror-qdrant -p NRestarts`) holds its `grey`
collections until someone resumes them by hand or starts Qdrant by hand.

A run in which Qdrant does not answer records `collections: []`, and a collection Qdrant will not describe (other than
"it is gone") appears only as a problem. Nothing is carried over from the previous record.

### When a settings change waits

A change to where the graph or the binary copy lives makes Qdrant rebuild every segment's graph in the background:
hours of background work on 41.6M passages, mostly disk reads and writes, with the keyword index still serving and no
embeddings redone (a change of the thread count alone does not rebuild). So the job applies a settings change only
when all of these hold, and otherwise leaves it and says why in the record, trying again 15 minutes later:

* the collection's embedding run is complete (the embed stage keeps HNSW off while it loads, on purpose);
* the collection is not `red` (a settings change would hide why it failed);
* no build or embedding run holds the build lock. The job only probes the lock and never holds it: a build starting
  while the job held it would exit 1 and wait an hour for its retry;
* for a change that rebuilds (the graph's `m`, `ef_construct` or `on_disk`, or the quantization): the restart count
  can be read and is at most 1 (the first two reasons for a hold, above), Qdrant holds no more than 90% of its memory
  cap, and free disk is at least `min_free_disk_gb`, because a rebuild rewrites segments.

A change that does not rebuild (only the optimizer thread count) waits until the collection is `green`, because any
collection update makes Qdrant stop the optimizations in flight, which would throw away the segment being rebuilt. A
`grey` collection has nothing in flight and is changed at once, unless it is held (a held collection gets no settings
update). Where the collection is not `grey`, the change cannot cause an out-of-memory kill, so it ignores the restart
count. If a settings update on a `grey` collection fails, the plain
resume follows in the same run.

### The problem sentences

A run with problems still exits 0 and writes its record. Each problem is one line in `wiki-mirror status`
(Maintenance section) and in the journal (`wiki-mirror logs maintain`); health shows the record as `maintenance`.

| Problem | What it means and what to do |
|---|---|
| `Qdrant did not answer: <ErrorType>` | The job could not list the collections. `systemctl status wiki-mirror-qdrant` and `wiki-mirror logs qdrant`. If Qdrant is still loading after a restart, it clears by itself on a later run. |
| `could not read Qdrant's unit: <why>` | `systemctl` is missing, slow or failing, or its reply had no `LoadState`. The rest of the record is still written, crashes and memory are not watched, and health is `degraded`. A `grey` collection is held until the unit can be read, because the restart count is unknown. |
| `Qdrant's unit wiki-mirror-qdrant.service is not installed` | The unit file is missing. Run `/opt/wiki-mirror/deploy/install.sh`. Health is `degraded` (`Qdrant's unit is not installed, so the maintenance job does not watch crashes and memory`), and a `grey` collection is held. |
| `Qdrant's unit is not running (ActiveState=<state>)` | Qdrant is stopped or failed. `systemctl start wiki-mirror-qdrant`, then `wiki-mirror logs qdrant` for why it stopped. |
| `could not read Qdrant's memory: <why>` | The cgroup files could not be read (`memory.stat`, `memory.max`, or the unit's cgroup path). Health cannot judge the memory share until this clears. |
| `<name>: Qdrant would not describe it (<ErrorType>)` | The collection was listed but could not be read. The collection is left out of the record's `collections`; this sentence is all the record says about it. Look at `wiki-mirror logs qdrant`. |
| `<name> is held: <unreadable>, so a crash loop cannot be ruled out; it is not resumed by itself until the count can be read` | The first reason of [the crash-loop guard](#the-crash-loop-guard). `<unreadable>` is `Qdrant's unit could not be read`, `Qdrant's unit wiki-mirror-qdrant.service is not installed` or `systemd did not report Qdrant's restart count`. Clears on the first run that can read the count. If Qdrant is healthy, `held` ends with the command to resume by hand. |
| `<name> is held: Qdrant has restarted itself after a failure <N> times since it was last started by hand (the last at <time>); resuming it may be what kills Qdrant again, so it is not resumed by itself` | The count is 2 or more. Find why Qdrant died (`wiki-mirror logs qdrant`; the memory cap and the disk are the usual causes), fix it, then resume by hand with the command at the end of the `held` text (`wiki-mirror status` shows it), or start Qdrant by hand, which resets the count. |
| `<name> is held: Qdrant holds <N>% of its memory cap; resuming would risk another out-of-memory kill` | Clears by itself when memory falls to 90% or less. If it persists, the cap is too small for the collection's layout. |
| `<name> is held: <disk sentence>` | Free disk is under `min_free_disk_gb`, or `the free disk space could not be checked (<ErrorType>)`. Free space; it clears by itself. |
| `<name> is still paused after a resume at <time>` | The collection was `grey`, the job resumed it, and it is `grey` again. The resume did not take: check `wiki-mirror logs qdrant` and `curl -s localhost:6333/collections/<name>` for `optimizer_status`. If Qdrant itself needs a restart, `systemctl restart wiki-mirror-qdrant`; the next run resumes the collection again. |
| `<name>: optimizer failed: <text>` (or `status is red, and Qdrant gave no optimizer error`) | Qdrant's optimizer stopped with an error. The job does not touch a red collection's settings. Read the text and `wiki-mirror logs qdrant` (a full disk and an out-of-memory kill are the usual causes); fix the cause, then a restart of Qdrant, and the job resumes it. |
| `<name>: settings not changed yet: <why>; they differ from the code's: <changes>` | A settings change is waiting (previous section); `<why>` says for what: the embedding run is not finished, the collection is red, the build lock is held, free disk is under the floor, `Qdrant holds <N>% of its memory cap and a rebuild would risk an out-of-memory kill`, the restart count cannot be read (`<unreadable>, so a crash loop cannot be ruled out`), `Qdrant has restarted itself after a failure <N> times since it was last started by hand, and a rebuild may be what kills it; it is tried again after a person has looked and started Qdrant by hand`, or `it waits for green (it is <status>): any collection update stops the optimizations in flight, which would throw away the segment being rebuilt`. Nothing to do unless it persists with a run that has finished: then the reason is the one to fix. |
| `<name>: could not resume: <ErrorType>` / `<name>: could not apply settings: <ErrorType>` | Qdrant refused the update or did not answer it. The update is repeated 15 minutes later; if it keeps failing, `wiki-mirror logs qdrant`. |

### Exit codes

0: the record was written, whether or not it holds problems. 1: the record could not be written (the state
directory is full or not writable) or the run raised; systemd shows the unit failed and the next run is the retry.
The job checks that it can write the record before it sends anything to Qdrant, because a run that cannot report must
not act: a run that exits 1 for this reason has changed nothing. The record is flushed to disk before it replaces the
old one.
2: a configuration error (the next run, 15 minutes later, hits it again until it is fixed).

### Following it

```bash
# wiki-mirror logs maintain           # the journal of the job: one line per run, one per action and problem
# systemctl start wiki-mirror-maintain  # a run now, instead of waiting for the timer
# systemctl list-timers wiki-mirror-maintain
```

`wiki-mirror status` shows the last run and its age, Qdrant's restarts after a failure and the latest, its memory
against its cap, the last action and each problem. It adds `the Qdrant unit could not be read` (in red, distinct from
`the Qdrant unit is not installed`) and `<name> is held: <reason>` (in red).
`/v1/wiki/health` turns `degraded` when the record is more than 60 minutes old (four missed runs: the timer is off or
the job is failing), when Qdrant was restarted after a failure within the last 24 hours, when Qdrant's anonymous
memory is above 90% of its cap, when a collection is held, and when the job could not read Qdrant's unit or
found it not installed ([API.md](API.md#get-v1wikihealth)). Health's "restarted after a failure within 24 hours" has two
blind spots: `systemctl reset-failed`, and a restart by hand during systemd's wait before an automatic restart. The
crash-loop guard does not depend on it.

### Enabling it

`install.sh` installs both units and enables the timer on every box. A box that updates itself
(`wiki-mirror-update.timer`) receives the units from `update.sh` but not the enabling, so once:

```bash
# systemctl enable --now wiki-mirror-maintain.timer
```

Until then `wiki-mirror status` warns "no maintenance record - enable the timer once", and health has
`maintenance: null` without being `degraded` for it.

### Changing the layout

The code is the truth for the semantic collection's settings. A setting changed by hand (a `PATCH` on the
collection) is put back within 15 minutes. To change the layout, change the constants in `embed.py` and ship the
change: the job applies it to the live collection once it may (above), and a collection `embed` creates later starts
with the same constants. Remember what it costs: a change to the graph or to where the binary copy lives is a
rebuild of every segment's graph in Qdrant, hours of CPU on this index. A decision of this size gets a row in
[DECISIONS.md](DECISIONS.md).

A changed layout constant is a rebuild of the semantic collection on **every box that runs that code**, started by
that box's next maintenance run after the code update: hours of disk work, with search unaffected. Unlike a change to
`deploy/FORMAT`, nothing holds such an update back. The waits above are the only brake: the embedding run finished, the
collection not `red`, the build lock free, the disk floor, and Qdrant under 90% of its memory cap.

## The monthly build

`wiki-mirror-build.timer` fires on the **2nd of each month at 05:00 UTC** (plus up to an hour of random
delay). Wikimedia Enterprise publishes on the 1st at 00:00 UTC and recommends consuming between 04:00 and 08:00
UTC; the 2nd gives a day of margin. `Persistent=true` runs a missed build at the next boot. Check it with
`wiki-mirror status` (Schedule) or `systemctl list-timers wiki-mirror-build.timer`.

**Not yet proven unattended.** The first build, in October 2026, was started by hand with `wiki-mirror build-now`.
The timer is scheduled, and its first unattended run is due on 2026-11-02. Until that has run, treat the monthly
rebuild as designed and tested, not as demonstrated: check `wiki-mirror status` after the 2nd.

Today the monthly job is a full build beside the live collection, then an alias swap (the rare-path design).
W3 (monthly in-place updates, [ROADMAP.md](ROADMAP.md)) will replace it with an update that reuses unchanged work
([DESIGN.md §12.6](DESIGN.md#126-the-monthly-update-w3)). On the 2nd the nightly update, if you enabled it, will
usually find the build running and defer.

## The nightly update

```bash
# wiki-mirror update
```

Root-run, fast-forward only, from `main`. It refuses while a build runs or if the checkout has local changes,
reinstalls the package under the pins in `constraints.txt`, syncs changed systemd units, restarts the **API** (not
Qdrant, not a build), waits for health, and **rolls back** to the previous commit if the new one does not come
up within 60 seconds. A firewall change in the checkout is reported, not applied (the template is rendered for your
`site.env` first, so only a real difference is reported): apply it deliberately with
`/opt/wiki-mirror/deploy/install.sh`. If `site.env` cannot be rendered, the update says so and carries on.

**The automatic nightly update is opt-in** ([D4](DECISIONS.md#d4) built it; the installer leaves it off). Enable it
with `install.sh --auto-update` or, any time, `systemctl enable --now wiki-mirror-update.timer`; turn it off with
`systemctl disable --now wiki-mirror-update.timer`. Re-running the installer never turns an enabled timer off.
**The trust model, plainly:** an enabled timer means your box runs whatever lands on the upstream `main` branch (or
on your fork's, if you cloned a fork), as root, every night, with a rollback only for code that fails to start.
Enable it only if you trust that branch and its maintainers that far. Without it, update by hand with
`wiki-mirror update` when you have read what changed.

Once enabled, `wiki-mirror-update.timer` runs `update.sh --unattended` at **02:20 local time**, plus up to five
minutes' random delay, `Persistent=true`. "Local" is the container's own time zone (see
[Run the installer](#5-run-the-installer)); check the next run with `systemctl list-timers wiki-mirror-update`. The
service runs as root (the checkout is root-owned), with no retry and no alert unit; a failure shows on
`wiki-mirror status`, `/v1/wiki/health` and `systemctl --failed`.

* **A box installed before the timer was opt-in** may already have it enabled; `systemctl list-timers
  wiki-mirror-update` shows. A new timer arriving with an update is installed but never enabled.
* **Every run is recorded** in `/var/lib/wiki-mirror/state/update.json` (`at`, `from`, `to`, `outcome`,
  `detail`, `unattended`). Health carries it as `update` (reporting only), and `wiki-mirror status` prints a
  `last update` row.

Outcomes:

| Outcome | Meaning | Unit exit |
|---|---|---|
| `updated` | New code is serving. | 0 |
| `current` | Nothing new on `main`. | 0 |
| `deferred` | A build is running or holds `state/build.lock`. Nothing changed. By hand this is a refusal. | 0 |
| `held` | The target commit changes `deploy/FORMAT` and built data exists. Nothing changed; a person decides. By hand it refuses unless `--accept-format-change`. | 0 |
| `failed` | Stopped before or without a clean rollback: dirty or diverged checkout, no route to GitHub. | 1 |
| `rolled_back` | The new commit installed but the API did not become healthy; the previous commit is back. | 1 |

**Guards.**

* A build blocks the update and the update blocks a build: `update.sh` takes the build's lock
  (`state/build.lock`, non-blocking) for the whole run, so a build cannot start in the middle of a package swap.
  A build that tries exits "a build is already running", and its hourly retry starts it.
* The **format guard**: `deploy/FORMAT` holds `store=<schema> chunker=<fingerprint> index=<format>`,
  and a test keeps it equal to the code. Before merging, `update.sh` compares the running commit's FORMAT with
  the target's. If they differ (or the running commit has none) and built data exists (any `*.db` in
  `articles/`), the update is held. With no built data a format change goes ahead. A parser version change is
  not a format change: it only affects the next build.
* The update that first brings a guard is run by the old script, which lacks it; the next one is protected.

**Both services run `python -I`** (no `PYTHONPATH`, no user site, no working directory on `sys.path`). The API
unit has `RestartPreventExitStatus=2`: a configuration error stops it instead of looping.

**Because `main` can ship to boxes with the nightly update enabled, never push untested work to `main`.** See
[CONTRIBUTING.md](../CONTRIBUTING.md).

### Refreshing `constraints.txt`

The pins are exact, so nothing moves until you move it. Do this when `pyproject.toml`'s dependencies change, or to
take a newer set deliberately. On the workstation:

1. Rebuild the test environment (`python3 -m venv .venv`, `.venv/bin/pip install -e ".[dev]"`).
2. List the closure of the base dependencies with their installed versions, one
   `name==version` per line (not pytest, ruff or mypy). `pip install --dry-run --report - "."` against
   a clean venv gives the same set under the current pins; they must agree.
3. Run `.venv/bin/pytest -q` against exactly that set.
4. Check every pin has a wheel for the box (CPython 3.13, Debian 12's glibc 2.36, x86_64), one pin at a time so
   every failure is listed rather than the first:

   ```bash
   $ grep -E '^[A-Za-z0-9._-]+==' deploy/constraints.txt | while read -r pin; do
   >   .venv/bin/pip install --dry-run --no-deps --ignore-installed --only-binary=:all: \
   >     --implementation cp --python-version 3.13 --abi cp313 --abi abi3 --abi none \
   >     --platform manylinux2014_x86_64 --platform manylinux2010_x86_64 --platform manylinux1_x86_64 \
   >     $(for m in $(seq 5 36); do printf -- '--platform manylinux_2_%s_x86_64 ' "$m"; done) \
   >     --target "$(mktemp -d)" --quiet "$pin" >/dev/null 2>&1 || echo "NOWHEEL $pin"; done
   ```

   Every `--platform` is listed because pip does not expand `manylinux_2_36` to the older tags. It must print
   nothing. A `NOWHEEL` line means that version has no wheel for the box: pin the previous one, because the
   installer installs wheels only and fails loudly rather than compile on the box.
5. Commit the new file with the change that needed it. `tests/deploy/` checks it is exact, covers the direct
   dependencies, meets `pyproject.toml`'s floors, and is what both scripts install with.

`qdrant-client` is the one pin that does not take the newest release. Qdrant supports a client at most one minor
version from its server, so the client stays on the minor of `QDRANT_VERSION` in `deploy/install.sh` (`pyproject.toml`
caps it, and a test compares the two). Upgrade the server and the client together.

## Network

Input is SSH and :8300 from the site's LAN (`LAN_CIDR` in [`site.env`](#4-the-site-file)), nothing else. Output is
per user: `wikimirror-sync` gets tcp 443
to the internet, meaning anywhere except the private and shared ranges (10/8, 172.16/12, 192.168/16, 100.64/10),
so the parse job, which handles editable web content, cannot reach a router or NAS admin page on the LAN
(Enterprise and the S3 bucket its downloads redirect to cannot be named by address) and, if you set
`EMBEDDING_HOST` and `EMBEDDING_PORT` in `site.env`, that one embedding server; root gets 80/443 (PyPI, GitHub),
git-over-SSH to GitHub's published ranges (for an SSH clone) and DHCP renewals (udp 68 to
67, LAN only); `_apt` gets tcp 80/443 over IPv4; `wikimirror` and `qdrant` get nothing. DNS goes to the LAN resolver
only.

**Refused is rejected, not dropped.** Everything the output chain does not admit is refused with an immediate
error (a TCP reset or an ICMP admin-prohibited) after being counted (`out_refused`, `service_refused`) and,
rate-limited, logged with the prefix `wiki-mirror-egress-refused` (or `...-service-refused` for `wikimirror`
and `qdrant`; `journalctl -k | grep service-refused` names the destination). A silent drop would turn a blocked
connection into a hang for the whole connect timeout. Input stays a drop.

## Failure modes and recovery

| Symptom | Cause | What to do |
|---|---|---|
| `wiki-mirror logs build` shows a login failure or 401 | Wrong Enterprise credentials, or the account was changed | Rewrite `enterprise.env` ([Install, step 3](#3-the-enterprise-account-and-sign-in)). The client logs in again on any 401 and refreshes its token for 90 days, so a persistent 401 is the credentials. |
| Build unit fails at once, "Assertion failed ... is not a mount point" | The NAS bind mount is missing (the host rebooted before NFS, or the share is down) | On the Proxmox host: `mount /mnt/wikipedia-mirror`, check `ls` shows the data, `pct reboot <ctid>`. The API is unaffected: it serves from local disk. |
| Installer or build says `wikimirror-sync` cannot write to the snapshots directory | NFS user mapping | Map all users on the share to the dataset's owner (on TrueNAS, **Mapall**; [Install, step 2](#2-storage-at-mntwikipedia)); restart the build. |
| The installer stops at "site file": missing, "still the shipped example", or a list of problems | `/etc/wiki-mirror/site.env` is absent, unedited, not owned by root or writable by others, holds a value that is not a valid network, address or port, or a `LAN_CIDR` that is not private, that your SSH session is outside of, or that this box has no address in | Edit or fix it ([Install, step 4](#4-the-site-file)) and run `install.sh` again. `install.sh --dry-run` lists every problem; `install.sh --show-firewall` prints what a valid file would produce. |
| The installer stops at "resolv.conf names a resolver outside the LAN" | A public resolver in `/etc/resolv.conf` (Proxmox or the DHCP lease put it there), or `LAN_CIDR` does not contain the real resolver | Pin the LAN resolver as the message says, or correct `LAN_CIDR`. |
| `wiki-mirror status` says `degraded` with an `error` row "<stage>: ... (see the build log)", and the build unit "last run FAILED" | A stage raised. The unit retries hourly, five times in a day, then stays failed | `wiki-mirror logs build` has the traceback. Fix the cause, then `wiki-mirror build-now` (clears the retry limit, resumes from the checkpoints). Exit 2 in the log is a configuration error and is not retried. |
| `wiki-mirror: build: a build is already running` (exit 1) | `state/build.lock` is held: a build, a hand-run stage, or the update | `wiki-mirror status` / `wiki-mirror logs build`. Nothing was changed. The lock goes with the process, so no stale lock survives a crash or reboot. |
| `systemctl status wiki-mirror-embed` says `failed`, or the unit is waiting to restart | The embedding run stopped: the embedding server was down or slow for over 15 minutes, refused the key (401, 403) or the model (404), or answered the wrong shape; or Qdrant failed. The unit retries 15 minutes later | `wiki-mirror logs embed` names the cause (never the key). A refusal does not fix itself: correct `embedding_url`, `embedding_model` or `/etc/wiki-mirror/embedding.env`, then `wiki-mirror embed-now`, which resumes from the checkpoint ([Interruptions and resuming](#interruptions-and-resuming)). Exit 2 is a configuration error (no `embedding_url`) and is not retried. |
| The embed unit shows `start-limit-hit` (`Result=start-limit-hit`) | Six starts failed within a day; the unit stays down rather than hammer a dead server | Fix the cause (previous row), then `wiki-mirror embed-now`: it clears the limit and resumes. Nothing is lost. |
| Embed stops "wikipedia_semantic_... exists but has no usable state" | The collection is there but `state/index_semantic_<born>.json` is missing, unreadable or another snapshot's. The run will not delete two days of vectors on a guess | Restore the state file from a backup of `/var/lib/wiki-mirror/state/` if you have one. Otherwise start over: `curl -X DELETE http://127.0.0.1:6333/collections/wikipedia_semantic_<born>`, then `wiki-mirror embed-now`. |
| Embed stops "was started with different settings (chunker / model / dimensions / text form ...)" | The embedding settings or the code changed since the collection was started, so new vectors would not mix with the old | Put the setting back (`embedding_model`, `embedding_dimensions` in `mirror.yaml`) and `wiki-mirror embed-now`, or accept the cost of starting over: delete the collection and its state file, as in the previous row. |
| Embed logs "the checkpoint at page id N ... does not match Qdrant" | Qdrant holds fewer (or more) points than the state recorded, usually after a power loss or host crash | Nothing: the run steps its checkpoint back and re-embeds from there. See [Interruptions and resuming](#interruptions-and-resuming). Repeated every restart means Qdrant keeps losing data; check the disk and `wiki-mirror logs qdrant`. |
| Build fails "not enough free disk to create ..." | Less than `min_free_disk_gb` (20) free when it went to create a store or collection | `df -h /`; free space (see "no space" below) or lower the floor in `mirror.yaml` if you judge it too high; `wiki-mirror build-now`. |
| Parse fails "(N corrupt files deleted; download again)" | A snapshot chunk was truncated or corrupt (an interrupted download, an NFS fault) | Nothing to repair: the build deleted the bad file and its `.etag`. `wiki-mirror build-now` downloads those chunks again (they count against the monthly budget). Repeats on the same chunk point at the NAS. |
| Parse fails "holds a different snapshot" or "is already built ... from a different chunk list" | The store of that date is finished, and Enterprise now lists different chunks for it | The build will not mix two versions. Delete that date's store (`/var/lib/wiki-mirror/articles/<identifier>_<date>.db`) only if it is not the serving or the previous one, then `wiki-mirror build-now`. |
| Mirror `degraded`, `wiki-mirror logs api` says the snapshot "is N days old, past the 40-day limit" | The monthly build did not land | `wiki-mirror status` (Schedule, build unit), `systemctl list-timers`; `wiki-mirror build-now`. |
| Mirror `degraded`, `wiki-mirror logs api` says the collection "was indexed with chunker ... but this code cuts with ..." | The code that cuts passages changed since the served index was built | Rebuild the index. The next monthly snapshot is cut by the running code and replaces the old index; to fix it sooner, delete the served `wikipedia_lexical_<born>` collection by hand (search is down until `wiki-mirror build-now` finishes). |
| Build stops "no space left on device" | `/` full: two stores or two collections side by side (about 100 GB peak) | `df -h /`; `du -sh /var/lib/wiki-mirror/*`. A finished build deletes stores and index states older than the serving and rollback snapshots, and superseded collections; if the disk is still full, delete by hand only article stores older than those two (`ls /var/lib/wiki-mirror/articles`), never the serving one. Then `wiki-mirror build-now`. |
| Qdrant restarts repeatedly, `wiki-mirror logs qdrant` shows it killed; health says "Qdrant was restarted after a failure at ..." or "Qdrant holds N% of its memory cap" | Memory: the cap in the unit (`MemoryMax`) reached, or the container's 32 GB. Only anonymous (pinned) memory counts toward a kill, not page cache. On 2026-10-23 the cap was 20G and the semantic index was held in RAM (about 22.7 GB at rest), which could not fit ([D8](DECISIONS.md#d8)) | `wiki-mirror status` (Maintenance: Qdrant's memory against its cap and the last restart); `systemctl show wiki-mirror-qdrant -p NRestarts`. Check the semantic collection is on the layout the code names: the job reports a difference as a problem and puts it back (see [The maintenance job](#the-maintenance-job)); a graph or binary copy in RAM again is the first suspect. For the keyword collection, check it still has `max_optimization_threads: 1` and `max_segment_size: 400000` (`curl -s localhost:6333/collections/wikipedia_lexical`); lower `storage.performance.max_search_threads` in `qdrant.yaml`; do not run a build and heavy search together. Qdrant recovers from its WAL after a kill and the build resumes. A restart can leave a collection paused: next row. |
| Semantic index reads "paused - the maintenance job resumes it within 15 minutes" (collection status `grey`), usually after Qdrant restarted | Qdrant 1.12 does not restart pending optimizations after a restart until some update arrives, so the graph build stops where it was | Nothing, unless the job holds the collection (next row): it resumes it within 15 minutes and records the action. To do it now, `systemctl start wiki-mirror-maintain`. If it is still paused after a resume ("<name> is still paused after a resume at ..."), see [the problem sentences](#the-problem-sentences). If the job is not running, enable its timer ([Enabling it](#enabling-it)). |
| Health `degraded` with `<name> is held by the maintenance job: <held>`; `wiki-mirror status` shows `<name> is held: <reason>` | The crash-loop guard stopped the job resuming a paused collection ([The crash-loop guard](#the-crash-loop-guard)). Most often systemd has restarted Qdrant after a failure 2 or more times since it was last started by hand (`systemctl show wiki-mirror-qdrant -p NRestarts`), so the work may be what killed it. A box whose unit already shows 2 or more holds its paused collections until someone acts | Fix the cause first (`wiki-mirror logs qdrant`: the memory cap, the disk). Then resume by hand with the command at the end of the `held` text: `runuser -u wikimirror-sync -- curl -X PATCH http://127.0.0.1:6333/collections/<name> -H 'Content-Type: application/json' -d '{"optimizers_config":{}}'`, or start Qdrant by hand, which resets the count and gives the job its allowance again. A hold for memory, disk or an unreadable count lifts by itself once that clears. |
| Health `degraded` with "the maintenance job could not read Qdrant's unit, so crashes and memory are not watched" | `systemctl` is missing, slow or failing, or its reply had no `LoadState` | `systemctl show wiki-mirror-qdrant -p LoadState`; `wiki-mirror logs maintain` names the reason. Clears on the next run that can read the unit. |
| Health `degraded` with "Qdrant's unit is not installed, so the maintenance job does not watch crashes and memory" | `wiki-mirror-qdrant.service` is missing | Run `/opt/wiki-mirror/deploy/install.sh`. |
| Health `degraded` with "the maintenance job last ran at ..., N minutes ago; its timer or the job has stopped", or `wiki-mirror status` warns that the timer is not enabled | The record is more than 60 minutes old: the timer is off, or every run is failing (exit 1: the record cannot be written) | `systemctl list-timers wiki-mirror-maintain`; `systemctl enable --now wiki-mirror-maintain.timer` if it is off; `wiki-mirror logs maintain` for a failing run (a full or unwritable `/var/lib/wiki-mirror/state`). The warning clears on the next successful run. |
| A client says the mirror is unavailable or gets 401 | Key mismatch or revoked, wrong URL, or the firewall | `curl -s -o /dev/null -w '%{http_code}\n' http://<mirror-host>:8300/v1/wiki/health` from the client should print 401 (reachable, key required); then check its key with `wiki-mirror key list`. |
| An agent gets 401 on `/mcp` | Key mistyped, revoked, or not added on this box | `wiki-mirror key list` shows who has a key. A lost key cannot be shown again: `wiki-mirror key revoke <name>`, then `wiki-mirror key add <name>`. |
| `wiki-mirror status` says `degraded`; `wiki-mirror logs api` says "the article store for snapshot <date tag> (collection wikipedia_lexical_...) cannot be opened" | A build moved the alias (or the collection's `applied_snapshot` moved) but that snapshot's store is missing, unfinished or damaged | The old snapshot is still serving. `ls /var/lib/wiki-mirror/articles`; `wiki-mirror build-now` repeats parse and index (it resumes). The API picks the result up within 30 seconds. |
| `wiki-mirror status` says `degraded`, or the serving section has a `problem`; `wiki-mirror logs api` says "wikipedia_lexical_... has no usable index state" | `state/index_lexical_<born>.json` is missing, unreadable or unfinished: the collection has no record of which store it matches. The API will not guess "the newest store": a snippet cut from the wrong text would be cited with the wrong revision | Restore the file from a backup of `/var/lib/wiki-mirror/state/`; failing that delete the collection (`curl -X DELETE http://127.0.0.1:6333/collections/wikipedia_lexical_<born>` in the container; search is down until the rebuild ends) and run `wiki-mirror build-now`. |
| `wiki-mirror.service` fails at start, exit status 2, the log names `clients.json` | The clients file is not valid (a hand edit) | Fix or move it aside; it holds only key hashes. Restore agents with `wiki-mirror key add <name>` (a new key each). While running, a bad edit is ignored: the log says "keeping the last good client list". |
| `wiki-mirror update` fails "could not fetch from origin" on a box cloned over SSH (a private fork) | GitHub changed its SSH addresses and the `github_git` set in the firewall is stale | If the repository is public, switch to HTTPS for good: `git -C /opt/wiki-mirror remote set-url origin https://github.com/<owner>/<repo>.git`. For a private fork, root also has 443: `git -C /opt/wiki-mirror remote set-url origin ssh://git@ssh.github.com:443/<you>/<fork>.git` lets `wiki-mirror update` fetch, but the installer accepts SSH from `github.com` only, so set it back to `git@github.com:<you>/<fork>.git` once the set is refreshed from `api.github.com/meta`, before you re-run `install.sh`. A box cloned over HTTPS (the default) is not affected. |
| Health `update` says `deferred` ("update to <commit> waits: the build ...") | The nightly update found a build running or the lock held (a semantic build can run for days). Nothing changed; exit 0 | Nothing: it tries again the next night. To update now, wait for the build (`wiki-mirror logs build`) or stop it (`systemctl stop wiki-mirror-build`, it resumes later) and run `wiki-mirror update`. |
| Health `update` says `held` ("format store=... -> ...") | The target commit changes `FORMAT` and built data exists, so restarting the API on it would serve unreadable or misaligned data until a rebuild of many hours. Nothing changed; exit 0 | Decide: leave it (the old code keeps serving, and the update stays held every night; it is not a fault), or accept it: `wiki-mirror update --accept-format-change`, then `wiki-mirror build-now`. |
| Health `update` says `rolled_back`, the update unit is `failed` | The new commit installed but the API did not become healthy within 60 s, or its install or restart failed. The previous commit is back and serving | `journalctl -u wiki-mirror-update -n 80` and `journalctl -u wiki-mirror -n 50` name why. The same commit is tried again the next night (and fails again); fix it on `main`, or `systemctl disable --now wiki-mirror-update.timer` until you have. |
| Health `update` says `failed` ("stopped unexpectedly (exit N)", "the checkout has local changes", "could not fetch from origin", "cannot fast-forward") | The update stopped before or without a clean rollback: a dirty or diverged checkout, no route to GitHub, or an unexpected shell error. Only a message saying the rollback was incomplete means the box may be in neither version | The journal line starting `update:` names it. `git -C /opt/wiki-mirror status` for a local change (commit, stash or discard it: someone edited production). If the rollback was incomplete: compare `git -C /opt/wiki-mirror log --oneline -1` with `/opt/wiki-mirror/.venv/.installed-commit`, then `wiki-mirror update` or `git reset --hard <good commit>` and the pip lines in `update.sh`. |
| `wiki-mirror: command not found` after `pct enter <ctid>` | `pct enter` gives a shell whose PATH lacks `/usr/local/bin` | `exec bash -l`, or use `/usr/local/bin/wiki-mirror`. |
| Locked out of SSH | A firewall mistake, often a `LAN_CIDR` that does not contain the machine you connect from | On the Proxmox host: `pct enter <ctid>`, then `nft flush ruleset`; fix `site.env` or the file and run `install.sh`. |
| Whole container lost | | Recreate the container and the bind mount, rewrite `/etc/wiki-mirror/site.env` (worth keeping a copy off the box), redo the Enterprise sign-in (and the deploy key, for a private fork), run `install.sh`, then give clients their keys (a *new* primary key is generated, and the client keys must be re-issued with `wiki-mirror key add`), then `wiki-mirror build-now`. Nothing on the box is irreplaceable: the snapshot is re-downloadable and everything else rebuilds from it. |

Where to look first, in order: `wiki-mirror status`, `wiki-mirror logs api`, `wiki-mirror logs build`, `wiki-mirror logs qdrant`,
`journalctl -u wiki-mirror-update`, `journalctl -k | grep wiki-mirror` on the Proxmox host.

## Uninstall

The installer puts everything in the places below and nothing else, so removing it is a short list. If the box is
dedicated, deleting the container or VM is simpler and removes the firewall too. As root on the box:

1. **Stop and disable the units** (the timers first, so nothing starts again):

   ```bash
   # systemctl disable --now wiki-mirror-update.timer wiki-mirror-build.timer wiki-mirror-maintain.timer
   # systemctl disable --now wiki-mirror.service wiki-mirror-build.service wiki-mirror-update.service wiki-mirror-maintain.service wiki-mirror-qdrant.service
   ```

   (`wiki-mirror-build.service`, `wiki-mirror-update.service` and `wiki-mirror-maintain.service` are oneshots; a
   "not loaded" message for an idle one is fine.)
2. **Remove the unit files:**

   ```bash
   # rm /etc/systemd/system/wiki-mirror.service /etc/systemd/system/wiki-mirror-qdrant.service
   # rm /etc/systemd/system/wiki-mirror-build.service /etc/systemd/system/wiki-mirror-build.timer
   # rm /etc/systemd/system/wiki-mirror-update.service /etc/systemd/system/wiki-mirror-update.timer
   # rm /etc/systemd/system/wiki-mirror-maintain.service /etc/systemd/system/wiki-mirror-maintain.timer
   # systemctl daemon-reload
   ```

3. **Remove the firewall.** Take the line `include "/etc/nftables.d/wiki-mirror.nft"` out of `/etc/nftables.conf`
   (edit the file; if the installer created `/etc/nftables.conf` it holds only that line and a `flush ruleset`), delete
   the ruleset file, and drop the live table, or the default-deny rules stay until the next reboot:

   ```bash
   # rm -f /etc/nftables.d/wiki-mirror.nft /etc/nftables.d/wiki-mirror.nft.previous
   # nft delete table inet wiki_mirror
   ```
4. **Remove the program, its data and its configuration:**

   ```bash
   # rm /usr/local/bin/wiki-mirror
   # rm -rf /opt/wiki-mirror /var/lib/wiki-mirror /etc/wiki-mirror
   ```

   `/var/lib/wiki-mirror` holds the article store, the Qdrant index and the build state (rebuildable, but it is the
   14-hour build). `/etc/wiki-mirror` holds `enterprise.env` (your Enterprise password) and `mirror.env` (the primary
   key) and `clients.json` (key hashes); deleting it revokes every client key. Copy out anything you want to keep first.
5. **Remove the users and group:**

   ```bash
   # userdel wikimirror
   # userdel wikimirror-sync
   # userdel qdrant          # also removes the group qdrant, which the installer made with it
   # groupdel wikimirror
   ```

   The installer created these three system users and the shared group `wikimirror`, with no home directories.
6. **The downloaded snapshots** are on your storage, not in the paths above: delete `/mnt/wikipedia/snapshots`
   (about 300 GB) if you want the space back. The mount itself (the NFS or bind-mount line in `/etc/fstab`, or the
   Proxmox `mp1` entry) is yours to remove; the installer never made it.
7. **If you cloned over SSH**, delete the deploy key (`/root/.ssh/wiki_mirror_deploy` and `.pub`) and remove it from
   the repository's Settings, Deploy keys on GitHub. The installer also added GitHub's host key to
   `/root/.ssh/known_hosts` if it was not there; leave it or remove that line.

**What the installer leaves behind**, on purpose: the Debian packages it installed (`git`, `curl`, `ca-certificates`,
`openssh-client`, `nftables`, `zstd`, `sqlite3`, `xz-utils`, and any build packages for Python; remove them with
`apt` if nothing else needs them), the CPython 3.13 it built from source under `/usr/local` (the interpreter in
`/usr/local/bin/python3.13` and its libraries), and the Qdrant binary `/usr/local/bin/qdrant`, which you can delete
(`rm /usr/local/bin/qdrant`). `nftables.service` stays enabled; with the include line gone it loads no wiki-mirror rules.

## Rules that are easy to get wrong

* **Live installs and `wiki-mirror build-now` are run by the operator**, not by a coding agent or CI.
* **Never put the Enterprise credentials in this repository**, in a log, or in a message. They live in
  `enterprise.env` on the box.
* **Never edit `clients.json` by hand**; use `wiki-mirror key`.
* **Do not run a build and heavy searches together** on a long semantic build; they compete for Qdrant's memory.
* A `held` update is a decision for a person, and `--accept-format-change` commits to a rebuild.
