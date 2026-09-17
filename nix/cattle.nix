# The Proxmox LXC flavour of services.gtd-mcp: the handful of settings the
# cattle contract (github:charlesbaynham/nix-proxmox-cattle) requires and that
# the portable module deliberately does not assume. Imported only by the LXC
# template output in flake.nix — a plain NixOS host never sees this file.
#
# All mkDefault, so ./deployment.nix (yours, if you made one) overrides any of
# it without fuss.
{ lib, ... }:
{
  services.gtd-mcp = {
    # All state under the cattle state volume. The rootfs is thrown away on
    # every deploy, so anything written outside /data is written to nowhere.
    stateDir = lib.mkDefault "/data/gtd-mcp";
    vaultDir = lib.mkDefault "/data/vault";

    # Seeded out of band, mode 0600, never in git and never in the image.
    environmentFile = lib.mkDefault "/data/secrets/gtd-mcp.env";

    # The container is reached from other machines on the LAN; its security
    # border is the router in front of it, not its own firewall.
    host = lib.mkDefault "0.0.0.0";
    openFirewall = lib.mkDefault true;

    # A state volume that failed to attach must stop the service, not let it
    # write to a rootfs that is about to be discarded.
    requiredUnits = lib.mkDefault [ "cattle-state-preflight.service" ];
  };
}
