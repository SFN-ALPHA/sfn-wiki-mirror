# Security policy

## Reporting a vulnerability

Please report a vulnerability **privately**, not in a public issue, pull request or discussion.

Use GitHub's private vulnerability reporting: on the repository page open **Security**, then **Report a
vulnerability**, and describe what you found. Include the version or commit, what an attacker needs (network
position, a client key, nothing), the steps to reproduce, and what it lets them do. Do not include a working
exploit against a box that is not yours, a real key, or Enterprise credentials.

You will get an acknowledgement, and a fix or a decision, as fast as one maintainer can manage; this is a small
project and there is no support contract. When a fix is released you will be credited, if you want to be.

## Scope

In scope:

* **The API and the MCP server** (`/v1/wiki/*`, `/mcp`): authentication bypass, a way to read or write anything
  beyond the two read-only tools, a key or a key hash leaking, request-id or log injection, a denial of service
  that one client key can cause against the others.
* **The installer and the updater** (`deploy/install.sh`, `deploy/update.sh`): anything that lets a local or remote
  party get code run as root, or makes the installer apply a firewall or file it should have refused.
* **The firewall** (`deploy/nftables/` and the site-file rendering): a way for the API user (`wikimirror`) to open
  an outbound connection other than the one to the embedding server described below, for Qdrant to open any, or
  for a host outside `LAN_CIDR` to reach SSH or the API.
* **Handling of untrusted text:** a path by which Wikipedia text is treated as instructions, or leaks credentials.

Out of scope: vandalised or wrong Wikipedia content (the replies label it untrusted; report that to Wikipedia),
weaknesses in a client you wrote, the contents of the public Wikipedia snapshot, and attacks that need root on the
box already. A LAN host with a valid key can use the service by design.

## Things worth knowing when you assess a report

* The service is built for a trusted LAN, not the public internet. Keys limit who uses it and tell the logs who
  asked; the data is public Wikipedia.
* **The API has one permitted outbound connection, and a key for it.** For the `semantic` and `hybrid` search modes
  the API process embeds the question itself, so the firewall lets the API user (`wikimirror`) connect to the
  embedding server's one address and port, and only when `site.env` names that server
  ([D10](docs/DECISIONS.md#d10)). Everything else it tries, DNS included, is refused and counted in
  `service_refused`; Qdrant is refused everything, with no exception. The API holds a key for that server,
  `EMBEDDING_API_KEY` in `/etc/wiki-mirror/mirror.env` (root:root 0600; systemd reads it before dropping privileges),
  and it is a different key from the bulk embedding run's (`embedding.env`), so either can be revoked alone. Issue it
  on the embedding server as a key that may embed and nothing more. The accepted cost: an attacker who takes over
  the API process can send requests to that one server with that key, limited to what the server lets that key do;
  they still cannot reach any other host or port or resolve a name, and what the API user may read or write on the
  box itself is unchanged. A box with no embedding server in `site.env` has no such rule, and the API user is
  refused every connection that leaves the box, as before (loopback, which reaches Qdrant, is admitted for every
  user). The key is never in a log, an error message or a reply, and the question is never logged.
* The nightly self-update is opt-in. A box that enables it runs whatever lands on its `main` branch as root; that
  is a documented trust decision, not a vulnerability by itself.

## Supported versions

Only the latest release and `main`.
