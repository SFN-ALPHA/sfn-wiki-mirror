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
* **The firewall** (`deploy/nftables/` and the site-file rendering): a way for the API user (`wikimirror`) or
  Qdrant to open an outbound connection, or for a host outside `LAN_CIDR` to reach SSH or the API.
* **Handling of untrusted text:** a path by which Wikipedia text is treated as instructions, or leaks credentials.

Out of scope: vandalised or wrong Wikipedia content (the replies label it untrusted; report that to Wikipedia),
weaknesses in a client you wrote, the contents of the public Wikipedia snapshot, and attacks that need root on the
box already. A LAN host with a valid key can use the service by design.

## Things worth knowing when you assess a report

* The service is built for a trusted LAN, not the public internet. Keys limit who uses it and tell the logs who
  asked; the data is public Wikipedia.
* The nightly self-update is opt-in. A box that enables it runs whatever lands on its `main` branch as root; that
  is a documented trust decision, not a vulnerability by itself.

## Supported versions

Only the latest release and `main`.
