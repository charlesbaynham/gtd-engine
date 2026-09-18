# Running the MCP server with Nix

Four ways to run `gtd-mcp`, in increasing order of commitment. Pick one; the
others cost you nothing by existing.

| You have | Use | What you get |
|---|---|---|
| Nothing but Python | `pip install -e ci -e mcp` | See [`../mcp/README.md`](../mcp/README.md) |
| Nix, any OS | `nix run .#gtd-mcp -- stdio --vault ~/gtd` | A server for one local assistant, nothing installed |
| A NixOS machine | `nixosModules.gtd-mcp` | A hardened systemd service on a host you already run |
| A Proxmox server | the LXC template on this repo's releases | A disposable container, rebuilt and replaced on every deploy |

Everything below is optional. If you skip all of it, the vault, the nightly
maintenance and the pip-installed MCP server work exactly as before.

## Ad hoc: `nix run`

```bash
nix run .#gtd-mcp -- stdio --vault ~/gtd
```

No installation, no service, no state. This is the right answer for a local
coding agent talking to a clone you already have on disk.

## A service on any NixOS machine

`nixosModules.gtd-mcp` is a plain NixOS module: no Proxmox, no assumptions
about where state lives, no secrets required. The smallest useful config is
two lines.

```nix
{
  inputs.gtd-engine.url = "github:charlesbaynham/gtd-engine";   # this repo

  # ...in your host's configuration:
  imports = [ inputs.gtd-engine.nixosModules.gtd-mcp ];
  services.gtd-mcp.enable = true;
}
```

That gives you a server on `127.0.0.1:8000` holding its vault at
`/var/lib/gtd-mcp/vault`, committing locally and pushing nowhere. Point it at a
real vault repo when you want it to sync:

```nix
services.gtd-mcp = {
  enable = true;
  remoteUrl = "https://gitlab.com/you/gtd.git";
  environmentFile = "/var/lib/secrets/gtd-mcp.env";   # holds GTD_PUSH_TOKEN
  allowedUsers = [ "you" ];                            # if anything else can reach it
  host = "0.0.0.0";
  openFirewall = true;
};
```

### Options

All of them live in [`gtd-mcp.nix`](gtd-mcp.nix) with descriptions; the ones
that matter:

| Option | Default | Notes |
|---|---|---|
| `remoteUrl` | `null` | null = local-only: never fetches, never pushes, just commits in `vaultDir` |
| `vaultDir` | `${stateDir}/vault` | Runtime state. Never baked into an image |
| `branch` | `"master"` | |
| `allowedUsers` | `[ ]` | Empty = no per-call check. Only safe if nothing untrusted can reach the port |
| `host` / `port` | `127.0.0.1` / `8000` | |
| `openFirewall` | `false` | |
| `environmentFile` | `null` | Mode-0600 file of `KEY=value` lines: `GTD_PUSH_TOKEN`, `GTD_WEBHOOK_SECRET` |
| `configFile` | `null` | A `KEY=value` file read *after* everything above, so a deployment retunes the image without rebuilding it |
| `requiredVars` | `[ "GTD_PUSH_TOKEN" ]` when `remoteUrl` is set, else `[ ]` | The unit refuses to start if one is empty or still `CHANGEME` |
| `requiredUnits` | `[ ]` | Units to depend on: a state-volume preflight, a mount, a VPN |

The defaults describe a local-only server that needs no secrets at all, so
`services.gtd-mcp.enable = true;` on its own always comes up.

### Secrets

The push token and the webhook secret only ever exist in `environmentFile`,
mode 0600, seeded by hand or by whatever secret tooling you use:

```
GTD_PUSH_USER=<the deploy token's username, or anything for a project access token>
GTD_PUSH_TOKEN=<a token with read_repository + write_repository>
GTD_WEBHOOK_SECRET=<random string, also pasted into the GitLab webhook config>
```

The token is never written to the Nix store, a git config or a command line:
`gtd_mcp.store` hands git an inline credential helper that reads
`GTD_PUSH_TOKEN` from the environment at the moment git asks for it.

A service with `requiredVars` set refuses to start when a value is missing or
still a placeholder. Coming up insecure is a worse failure than not coming up.

### Configuration at runtime, from `configFile`

Everything that varies between *deployments* rather than between *images* —
`remoteUrl`, `branch`, `allowedUsers`, `requiredVars`, the poll interval, the
commit authorship — is also settable from `configFile`, a `KEY=value` file on
the machine. The unit's environment is three layers, later winning over
earlier: the image's own settings, then `environmentFile`, then `configFile`.

That is what lets **one** published image serve any vault, and it is the whole
reason this repo builds the template rather than each vault building its own.
`nix/config.env.example` is the annotated file; the keys are the `GTD_*`
environment variables named in the options above.

Paths, bind address and port are deliberately *not* in that layer: the unit's
sandbox (`ReadWritePaths`) and the firewall hole are built around them, so
overriding them at runtime would only make the unit disagree with itself.

## A Proxmox LXC container

This is the cattle pattern from
[`nix-proxmox-cattle`](https://github.com/charlesbaynham/nix-proxmox-cattle):
the whole OS is built by Nix, shipped as an LXC template, and deployed by
*replacing* the container. No `nixos-rebuild` on a running box, no drift. Read
that repo's README for the contract; the parts specific to this service are:

- **State** lives on a Proxmox volume mounted at `/data`, with the vault clone
  at `/data/vault`, this deployment's settings at `/data/config.env` and its
  secrets at `/data/secrets/gtd-mcp.env`. The shared module's preflight unit
  fails the boot if that mount is missing, which is why
  [`cattle.nix`](cattle.nix) makes the service `require` it.
- **Health** is `GET /health` on port 8000.
- **Auth** is `allowedUsers`, re-checked per tool call. The container itself
  does no TLS: terminating TLS is the border router's job.

### Get it

You do not build it. This repo's `lxc-template` workflow builds the template on
every change to `ci/`, `mcp/`, `nix/` or the flake and attaches it to a release,
named `gtd-mcp-lxc-proxmox-*.tar.xz`; point your deployer at the newest release
carrying that asset. `nix build .#proxmoxLxcTemplate` builds the same thing
locally when you want to inspect it.

The image is **generic**: no vault URL, no branch, no caller list. There is
nothing per-deployment in it to go stale, and rebuilding it is never how you
change which vault it serves.

### Configure it

Copy [`config.env.example`](config.env.example) to `/data/config.env` on the
container and edit: the vault URL, the branch, who is allowed to call it. Then
seed `/data/secrets/gtd-mcp.env` with the push token, and start the service.

⚠️ **The container fails to boot its service until both files exist**, on
purpose: a container that came up with no vault and no caller check, because
its state volume was never seeded, would be a deploy that looked like it
worked. A deploy loop that health-checks the port rolls back instead.

The webhook, if you use one, points at `https://<your host>/hooks/gitlab` with
**Push events** and the branch filter set to your default branch; that path must
not sit behind an OAuth proxy, because GitLab cannot do OAuth. Add
`GTD_WEBHOOK_SECRET` to `GTD_REQUIRED_VARS` in `config.env` so a missing secret
fails the deploy rather than silently never syncing on push.

Baking the settings into an image instead — for a fork that deploys one vault
and prefers it that way — is [`deployment.nix.example`](deployment.nix.example),
copied to `deployment.nix` beside it. The flake picks it up if it exists.
`config.env` still overrides it, since it is read last.

### What is deliberately not here

A VMID, an IP address, a storage pool or a hostname. Those belong to whatever
inventory you deploy from, not to this repo.
