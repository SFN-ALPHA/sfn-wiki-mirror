# Mirror deployment

**What this directory is:** everything that installs and runs the Wikipedia mirror
(`src/wiki_mirror/`) on its own host or container: the installer, the updater, the operator
command, the systemd units, the firewall (a template) and the config templates, including
`site.env.example`, the per-site settings (the LAN, and optionally an embedding server) that the firewall is built
from.

**How it fits:** the mirror host is the only machine that downloads from Wikimedia; clients on the
LAN reach its API and MCP server on :8300 with a bearer key. The reference deployment is an LXC
(hostname `wikipedia-mirror`, Debian 12, 6 vCPU, 32 GB RAM) on a Proxmox host. The addresses in the
examples below (LAN `192.168.1.0/24`, router and resolver `192.168.1.1`, this box `192.168.1.50`) are
examples; substitute your own. The values that the firewall needs are set in one place, the site file
below. The installer owns the box's firewall (default-drop), so give it a dedicated container or VM, not a shared
host or a Docker host.

See [docs/DESIGN.md](../docs/DESIGN.md) sections 4-6 and
[docs/OPERATIONS.md](../docs/OPERATIONS.md) for the full procedure, the NAS and Proxmox side
and the failure modes.

## Order of operations

Sizing, in one place ([docs/SIZING.md](../docs/SIZING.md) has the arithmetic): a Debian 12 or 13 x86_64 box with
6 vCPU (the parse stage uses 5 workers), 8 GB RAM minimum and 32 GB advised, **250 GB of fast local disk**, and
about 300 GB of bulk storage for the downloaded snapshots. Time: about 30 minutes to install, about 151 GB to
download, about 14 hours for the first build. You also need a free Wikimedia Enterprise account
(<https://enterprise.wikimedia.com/>; docs/OPERATIONS.md, Install step 3).

1. **Get the files onto the box.** From a checkout, `scp deploy/install.sh deploy/site.env.example
   root@192.168.1.50:/root/`. With no root SSH, `pct push <ctid> deploy/install.sh /root/install.sh --perms 0755`
   and `pct push <ctid> deploy/site.env.example /root/site.env.example` from the Proxmox host do the same.
2. **Storage.** `/mnt/wikipedia` must be a mount point (the build unit refuses to run otherwise). Either mount an
   NFS dataset on the Proxmox host and bind it in with `pct set <ctid> -mp1 <host path>,mp=/mnt/wikipedia,backup=0`
   (map every user and group on the share to the dataset owner: on TrueNAS set **Mapall** user and group, not
   Maproot, since the container's writes arrive as uid 100000+N and Maproot maps only uid 0), or, with one local disk,
   bind-mount a directory (`/srv/wikipedia /mnt/wikipedia none bind 0 0` in `/etc/fstab`, or `pct set <ctid> -mp1
   /srv/wikipedia,mp=/mnt/wikipedia,backup=0` for an unprivileged container). Pin the resolver with
   `pct set <ctid> --nameserver 192.168.1.1`. Details: docs/OPERATIONS.md, Install step 2.
3. Inside the container as root, write `/etc/wiki-mirror/enterprise.env` (docs/OPERATIONS.md has the `read -r -s`
   command).
4. Write the site file `/etc/wiki-mirror/site.env`: copy `/root/site.env.example` there and edit it. `LAN_CIDR` is
   required and ships commented out: remove the `# ` and write your own network. `EMBEDDING_HOST` and `EMBEDDING_PORT` are optional (both or neither): they only open one egress rule for
   the planned semantic index (W2, see docs/ROADMAP.md), and nothing uses an embedding server today. The installer
   refuses to go on while the file is missing or still the example, and stops on any value that is not a valid
   network, address or port. The file must be root-owned, 0644 and not a symbolic link, `LAN_CIDR` must be a private
   network of `/8` or longer (10/8, 172.16/12, 192.168/16 or 100.64/10), and both this box and your SSH session must
   be inside it.
5. `/root/install.sh --dry-run`, then `/root/install.sh` (add `--auto-update` to turn on the nightly self-update,
   below). A first run builds Python 3.13 from source (15-25 minutes). It clones this repository over anonymous
   HTTPS, so no key is needed. For a private fork, pass an SSH URL with `--repo git@github.com:<you>/<fork>.git`:
   that needs a read-only deploy key at `/root/.ssh/wiki_mirror_deploy`, and the installer prints how to create and
   register it.
6. `wiki-mirror status`, then `wiki-mirror build-now` when you choose. The installer never starts a build.
7. Give `WIKI_MIRROR_API_KEY` (the primary key, in `/etc/wiki-mirror/mirror.env`) to the primary client, or give
   every client a key of its own: `wiki-mirror key add <name>`, then point the client at
   `http://<mirror-host>:8300/mcp` or the HTTP API (docs/MCP.md, docs/API.md). The service picks the
   key up without a restart.

## Files

| File | Purpose |
|---|---|
| `install.sh` | Idempotent installer, `--dry-run`, `--adopt-config`, `--auto-update` (enable the nightly update), `--repo <url>`, `--show-firewall` (print the ruleset the site file produces; changes nothing). Pinned Python and Qdrant with sha256. Reads and validates the site file, then renders the firewall from it. |
| `site.env.example` | The per-site settings: `LAN_CIDR` (required), and `EMBEDDING_HOST` with `EMBEDDING_PORT` (optional, both or neither; commented out in the example). Installed as `/etc/wiki-mirror/site.env` on a first run, which then stops until you edit it; an edited file is never overwritten. |
| `constraints.txt` | Every Python dependency of the install at an exact version, installed from wheels only (`-c`, `--only-binary=:all:`). Refresh procedure in its header. |
| `update.sh` | Root-run fast-forward update, reinstall, API restart, rollback on an unhealthy start. Runs by hand (`wiki-mirror update`) or nightly with `--unattended`. Holds the build lock while it swaps the package, defers while a build runs, holds a commit that changes the stored data's format (`FORMAT`), and records every run in `state/update.json`. |
| `FORMAT` | One line, `store=<schema> chunker=<fingerprint> index=<format>`, that `update.sh` compares between the running commit and the target. A test keeps it equal to the code's constants; change the code, change this file. |
| `wiki-mirror` | Operator command: `status`, `build-now`, `logs`, `update [--accept-format-change]`, `search`, `key add\|list\|revoke`. Symlinked to `/usr/local/bin/wiki-mirror`. |
| `mirror.yaml` | Settings template (every key commented at its default). Installed once, never overwritten. |
| `qdrant.yaml` | Qdrant settings, each non-default justified. Installed once, never overwritten. |
| `systemd/wiki-mirror-qdrant.service` | Qdrant, user `qdrant`, loopback only. |
| `systemd/wiki-mirror.service` | The API, user `wikimirror`, read-only data, no egress. Python in isolated mode (`-I`), run from `/`. |
| `systemd/wiki-mirror-build.service` | One build, user `wikimirror-sync`, low priority, 5 GB cap, no start timeout. A failure is retried hourly, five times in a day, then it stays failed. |
| `systemd/wiki-mirror-build.timer` | The 2nd of each month, 05:00 UTC (Enterprise publishes on the 1st). |
| `systemd/wiki-mirror-update.service` | One unattended update (`update.sh --unattended`), as root (the checkout is root-owned), with the filesystem read-only except where the updater writes. No retry: a failure has rolled itself back. |
| `systemd/wiki-mirror-update.timer` | Every night at 02:20 local time (the container's time zone decides when: set it with `timedatectl set-timezone`), up to 5 minutes late, `Persistent=true`. Installed but **not enabled** unless you pass `--auto-update` to the installer or run `systemctl enable --now wiki-mirror-update.timer`. |
| `nftables/wiki-mirror.nft` | A TEMPLATE: default-deny both ways; egress scoped per user (`meta skuid`). Refused egress is rejected (fails at once), not dropped (hangs). Its three `define` lines take the site's values; `install.sh` renders it to `/etc/nftables.d/wiki-mirror.nft`. Never load the template with `nft -f`. |

## Identities

| User | Runs | Network |
|---|---|---|
| `wikimirror` | the API | none out; :8300 in from the LAN |
| `wikimirror-sync` | download, parse, index | tcp 443 to the internet (private and shared ranges excluded), the optional embedding server (`site.env`); holds `enterprise.env` |
| `qdrant` | Qdrant | none out; loopback only |
| `root` | install, update | tcp 80/443 anywhere, git-over-SSH to GitHub, DHCP renewals to the LAN |
| `_apt` | apt's download worker (it drops privileges to this user) | tcp 80/443 anywhere, ipv4 |

The checkout `/opt/wiki-mirror` is root-owned; no service account can change the code it
runs. `wikimirror` and `wikimirror-sync` share the group `wikimirror`, so the API reads what the build writes.

Keys: the primary key (client name `primary`) is `WIKI_MIRROR_API_KEY` in `mirror.env` (root, 0600,
read by systemd). Every other client's is a SHA-256 hash in `clients.json` (root:wikimirror 0640, so the API can check keys and
cannot learn one), written only by `wiki-mirror key`.

## Constraints worth knowing

* Qdrant and the article store are on **local SSD**; only downloaded snapshots live on the NAS.
* `vm.max_map_count` (a kernel setting, only changeable on the Proxmox host) may need raising
  before the planned semantic index (W2, see docs/ROADMAP.md): `sysctl -w vm.max_map_count=1048576` on the host.
* The keyword index is the Qdrant collection `wikipedia_lexical_<born>` (named for the snapshot it
  was first built from) behind the alias `wikipedia_lexical`. Which snapshot it currently reflects
  is `applied_snapshot` in `state/index_lexical_<born>.json`, which the API reads to pick the
  matching article store; a collection without a usable state file is not served (`wiki-mirror status`
  says `degraded`). `wiki-mirror status` shows both the snapshot date and the applied snapshot tag.
* Only one build runs at a time (a lock on `state/build.lock`); a second `build` exits 1 at once.
* The build refuses to create an article store or a Qdrant collection with less than
  `min_free_disk_gb` (20) free.
* The nightly self-update is opt-in (decision D4, 2026-10-17, amended 2026-10-19): enabling it means the box runs
  whatever lands on the upstream `main` (or your fork's) as root; without it, update by hand with
  `wiki-mirror update`. Each run ends as one of `updated`, `current`,
  `deferred` (a build is running or holds `state/build.lock`: try again tomorrow, exit 0), `held` (the
  target commit changes `FORMAT` and built data exists: a person decides, exit 0), `failed` or
  `rolled_back` (exit 1). The last one is on `/v1/wiki/health` (`update`), in `wiki-mirror status` and in
  `state/update.json`. To turn the timer on later: `systemctl enable --now wiki-mirror-update.timer`.
* `FORMAT` must change in the same commit as `store._SCHEMA_VERSION`, `passages.CHUNKER_VERSION` or the
  chunk sizes, or `index.INDEX_FORMAT` (bump that by hand for collection naming, payload fields or the
  index-state layout). The parser version is deliberately not in it: a parser change only affects the
  next build, not what is served.
* From `pct enter <ctid>` the shell's PATH lacks `/usr/local/bin`: run `exec bash -l`, or use
  `/usr/local/bin/wiki-mirror`. Over SSH `wiki-mirror` is found as usual.
* A firewall change is an operator action: `update.sh` reports a ruleset that differs from the installed one
  (rendered for your site file first, so the template itself never causes a false report), `install.sh`
  applies it (after `nft -c` on the whole configuration; if that check fails it puts the previous file back, so
  the next boot never loads a ruleset that failed).
* The site file is data, not code: `install.sh` accepts only the three keys (the embedding pair optional), each with a strictly valid IPv4
  network (no host bits, `/8` or longer, inside 10/8, 172.16/12, 192.168/16 or 100.64/10), IPv4 address or
  port, and stops on anything else, because the values land in a ruleset that loads as root. It also refuses a
  file that is not owned by root, is writable by group or others, or is a symbolic link, and a `LAN_CIDR` that
  the SSH session running it is outside of or that this box has no global IPv4 address in (the firewall would
  lock the next connection out). See docs/OPERATIONS.md, "The site file".
