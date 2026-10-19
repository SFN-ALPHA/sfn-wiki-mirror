# Operations

**Audience:** whoever runs the box. Commands match `deploy/`; if a script and this file disagree, the
script is right, so fix this file. Elsewhere in the docs the mirror's address is written `<mirror-host>`.

**The addresses in this guide are examples.** Substitute your own wherever you see one:

| What | Example used here | Yours |
|---|---|---|
| The LAN (clients, this box, the NAS, the Proxmox host) | `192.168.1.0/24` | `LAN_CIDR` in `site.env` |
| Your router, which is also the LAN's DNS resolver | `192.168.1.1` | the resolver the container will use |
| This box (reserved in your router's DHCP server) | `192.168.1.50` | `<mirror-host>` |
| The embedding server (optional; for the planned semantic index, W2, see [ROADMAP.md](ROADMAP.md)) | `192.168.1.60`, port `8080` | `EMBEDDING_HOST`, `EMBEDDING_PORT` in `site.env` |
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
| Services | `wiki-mirror-qdrant`, `wiki-mirror` (API), `wiki-mirror-build` (oneshot) |
| Timers | `wiki-mirror-build.timer` (monthly), `wiki-mirror-update.timer` (nightly; opt-in, see [The nightly update](#the-nightly-update)) |

### Memory and disk guidance

Memory, with the box at 32 GB:

| Process | Cap | Notes |
|---|---|---|
| Qdrant | `MemoryMax=20G` | The first full keyword build peaked at 5.1 GB of anonymous memory during segment merges and rests at 4.3 GB. The semantic index (W2) adds about 5.3 GB of binary vectors and the HNSW graphs in RAM, so the cap is 20G and the container needs 32 GB ([the first embedding run](#the-semantic-index-w2-the-first-embedding-run)). |
| API | `MemoryMax=1500M` | |
| Build | `MemoryMax=5G` | `Nice=10`, idle I/O priority: a build never makes search slow. |
| Embed | `MemoryMax=2G` | Same priorities; holds only a few windows of vectors. |

`systemctl status` shows a `Memory:` line that includes reclaimable page cache. To judge Qdrant, read anonymous
memory instead:

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
| `EMBEDDING_HOST` | no | IPv4 address of an Ollama-compatible embedding server, for the planned semantic index (W2, see [ROADMAP.md](ROADMAP.md)). It opens one egress rule: the only LAN host the build user (`wikimirror-sync`) may open a connection to. Nothing uses an embedding server today, so leave it out unless you are preparing for W2. | `192.168.1.60` |
| `EMBEDDING_PORT` | no | That server's TCP port. Give both `EMBEDDING_HOST` and `EMBEDDING_PORT`, or neither; there is no default. | `8080` |

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
| `wiki-mirror status` | Services, the mirror's health, the build unit, both timers, the checkout's commit, the firewall counters. Exits non-zero if the API does not answer. |
| `wiki-mirror build-now` | Starts the build unit in the background and returns. Resets the retry limit. Refuses if a build is running. |
| `wiki-mirror embed-now` | Starts the semantic index's embedding run ([below](#the-semantic-index-w2-the-first-embedding-run)) in the background and returns. Resets the retry limit. Refuses if a run is going. |
| `wiki-mirror logs [api\|build\|embed\|qdrant]` | Follows a unit's journal (default `api`). For an idle build or embed unit it prints the last 100 lines instead. |
| `wiki-mirror update [--accept-format-change]` | The update ([below](#the-nightly-update)). |
| `wiki-mirror search "<query>" [limit]` | A real query through the real API, default 5 results. |
| `wiki-mirror key add <name>` / `list` / `revoke <name>` | Agent keys ([API.md](API.md#client-keys)). |

The Python entry point behind it is `python -m wiki_mirror <download|parse|index|build|embed|serve|status|key>`.
`download`, `parse` and `index` run one stage by hand, for resuming one that failed. Exit codes: 0 success, 1 a
stage failed, 2 a configuration or usage error (systemd does not retry 2).

Settings are in `/etc/wiki-mirror/mirror.yaml` (every key commented at its default; an unknown key
fails the start). The primary key is never in it: the loader refuses an `api_key` there.

### What healthy looks like

`wiki-mirror status` should read: both services `active`; the build unit `idle, last run ok`; mirror `status ok` with
a snapshot date (`empty` before the first build, `building` during one, `degraded` if something is wrong); both
timers enabled; a `last update` row whose outcome is `updated` or `current`; and **`service_refused` at 0**.
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
API and the keyword index are untouched while it runs.

Before the first run:

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
3. **Set `embedding_url`** in `/etc/wiki-mirror/mirror.yaml` (the server's origin, e.g. `http://192.168.1.60:8080`;
   the firewall must admit `wikimirror-sync` to it: `EMBEDDING_HOST` and `EMBEDDING_PORT` in `site.env`, applied
   with `install.sh`). The other `embedding_*` keys have the measured defaults (16 requests in flight, 8 texts per
   request); `embedding_headers` can carry a background-priority header such as `X-Priority: 3` if the server has
   one. Without `embedding_url` the command exits 2.
4. **Give the container 32 GB of RAM** (`pct set <ctid> -memory 32768` on the host, live for an LXC) and **restart
   Qdrant to take its new cap** (`MemoryMax` is now 20G; `wiki-mirror update` installs the unit, then
   `systemctl restart wiki-mirror-qdrant`). The binary vectors (about 5.3 GB) and the HNSW graphs live in RAM next to
   the keyword index's 4.3 GB. Disk needs about 95 GB more than today; the run refuses to create the collection below
   `min_free_disk_gb`.

Then:

```bash
# wiki-mirror embed-now          # starts the unit in the background and returns
# wiki-mirror logs embed         # follow it: a progress line each minute (articles, passages/s, time left)
```

* **It resumes on its own after a failure.** The unit waits 15 minutes and starts again, six starts in a day, and
  each resume repeats only the windows that were in flight (`state/index_semantic_<born>.json` holds the
  checkpoint). After that it stays failed until `wiki-mirror embed-now`. A server that refuses the key (401, 403) or
  does not know the model (404) fails at once and says which; a busy or unreachable one is retried for 15 minutes
  per request first.
* **It holds the build lock for the whole run**, so the monthly build waits (exits 1, "a build is already running")
  until the embedding is done, and `wiki-mirror update` defers the same way.
* **It finishes by switching HNSW on and pointing the alias `wikipedia_semantic` at the collection.** Qdrant then
  builds the graphs in the background, one segment at a time, which takes longer than the run. Watch
  `curl -s localhost:6333/collections/wikipedia_semantic` until its `status` is `green`.
* **It refuses to mix vectors.** If the chunker, model, dimensions or text form recorded in the state differ from
  the running configuration, it stops and says so; delete the collection and the state file to start over. A
  collection with no usable state is never deleted for you.
* **A failure does not turn health `degraded`:** the embed stage never writes the build state.

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
| Build fails "not enough free disk to create ..." | Less than `min_free_disk_gb` (20) free when it went to create a store or collection | `df -h /`; free space (see "no space" below) or lower the floor in `mirror.yaml` if you judge it too high; `wiki-mirror build-now`. |
| Parse fails "(N corrupt files deleted; download again)" | A snapshot chunk was truncated or corrupt (an interrupted download, an NFS fault) | Nothing to repair: the build deleted the bad file and its `.etag`. `wiki-mirror build-now` downloads those chunks again (they count against the monthly budget). Repeats on the same chunk point at the NAS. |
| Parse fails "holds a different snapshot" or "is already built ... from a different chunk list" | The store of that date is finished, and Enterprise now lists different chunks for it | The build will not mix two versions. Delete that date's store (`/var/lib/wiki-mirror/articles/<identifier>_<date>.db`) only if it is not the serving or the previous one, then `wiki-mirror build-now`. |
| Mirror `degraded`, `wiki-mirror logs api` says the snapshot "is N days old, past the 40-day limit" | The monthly build did not land | `wiki-mirror status` (Schedule, build unit), `systemctl list-timers`; `wiki-mirror build-now`. |
| Mirror `degraded`, `wiki-mirror logs api` says the collection "was indexed with chunker ... but this code cuts with ..." | The code that cuts passages changed since the served index was built | Rebuild the index. The next monthly snapshot is cut by the running code and replaces the old index; to fix it sooner, delete the served `wikipedia_lexical_<born>` collection by hand (search is down until `wiki-mirror build-now` finishes). |
| Build stops "no space left on device" | `/` full: two stores or two collections side by side (about 100 GB peak) | `df -h /`; `du -sh /var/lib/wiki-mirror/*`. A finished build deletes stores and index states older than the serving and rollback snapshots, and superseded collections; if the disk is still full, delete by hand only article stores older than those two (`ls /var/lib/wiki-mirror/articles`), never the serving one. Then `wiki-mirror build-now`. |
| Qdrant restarts repeatedly, `wiki-mirror logs qdrant` shows it killed | Memory: `MemoryMax=20G` reached, or the container's 32 GB | `systemctl show wiki-mirror-qdrant -p NRestarts`; check the collection still has `max_optimization_threads: 1` and `max_segment_size: 400000` (`curl -s localhost:6333/collections/wikipedia_lexical`); lower `storage.performance.max_search_threads` in `qdrant.yaml`; do not run a build and heavy search together. Qdrant recovers from its WAL after a kill and the build resumes. |
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
   # systemctl disable --now wiki-mirror-update.timer wiki-mirror-build.timer
   # systemctl disable --now wiki-mirror.service wiki-mirror-build.service wiki-mirror-update.service wiki-mirror-qdrant.service
   ```

   (`wiki-mirror-build.service` and `wiki-mirror-update.service` are oneshots; a "not loaded" message for an idle one
   is fine.)
2. **Remove the unit files:**

   ```bash
   # rm /etc/systemd/system/wiki-mirror.service /etc/systemd/system/wiki-mirror-qdrant.service
   # rm /etc/systemd/system/wiki-mirror-build.service /etc/systemd/system/wiki-mirror-build.timer
   # rm /etc/systemd/system/wiki-mirror-update.service /etc/systemd/system/wiki-mirror-update.timer
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
