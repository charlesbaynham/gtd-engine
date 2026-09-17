# The gtd-mcp service: the MCP server over streamable-HTTP, holding its own
# clone of the vault.
#
# Nothing here assumes Proxmox, or any particular host. Every path, URL and
# user list is an option, and the defaults describe a single-machine,
# local-only server that needs no secrets at all. `./cattle.nix` layers the
# Proxmox LXC specifics on top of this; a NixOS box, VM or container can
# import this module on its own.
{ config, lib, pkgs, ... }:

let
  cfg = config.services.gtd-mcp;

  # Runs as the service user (no "+"): writes $HOME/.gitconfig. A push token
  # is never configured here — gtd_mcp.store hands it to git through an
  # inline credential helper reading GTD_PUSH_TOKEN from the environment, so
  # it never reaches disk or `ps`.
  gitConfigScript = pkgs.writeShellScript "gtd-mcp-git-config" ''
    set -eu
    git config --global user.name "''${GTD_GIT_NAME:-GTD MCP}"
    git config --global user.email "''${GTD_GIT_EMAIL:-gtd-mcp@noreply}"
    git config --global safe.directory ${cfg.vaultDir}
  '';

  # The contract: refuse to start rather than come up insecure or broken.
  # Values are already in the environment via EnvironmentFile by the time
  # this runs. Only the variables this configuration actually depends on are
  # checked, so a local-only server has nothing to seed.
  checkSecretsScript = pkgs.writeShellScript "gtd-mcp-check-secrets" ''
    set -eu
    for var in ${lib.concatStringsSep " " cfg.requiredSecrets}; do
      val="$(eval printf '%s' "\$$var")"
      if [ -z "$val" ] || [ "$val" = "CHANGEME" ]; then
        echo "gtd-mcp: $var is missing or still a placeholder${
          lib.optionalString (cfg.environmentFile != null) " in ${cfg.environmentFile}"
        }" >&2
        exit 1
      fi
    done
  '';
in
{
  options.services.gtd-mcp = {
    enable = lib.mkEnableOption "the GTD MCP server";

    package = lib.mkOption {
      type = lib.types.package;
      description = "The gtd-mcp Python application derivation.";
    };

    stateDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/gtd-mcp";
      description = "Service state: HOME, the git config, and by default the vault clone.";
    };

    vaultDir = lib.mkOption {
      type = lib.types.str;
      default = "${cfg.stateDir}/vault";
      defaultText = lib.literalExpression ''"''${config.services.gtd-mcp.stateDir}/vault"'';
      description = ''
        Where the vault clone lives. Runtime state, never baked into an image:
        with remoteUrl set the service clones into it on first start.
      '';
    };

    remoteUrl = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "https://gitlab.com/you/gtd.git";
      description = ''
        Git HTTPS URL of the vault repo. null — the default — means
        local-only: the service never fetches or pushes, and simply commits
        to whatever repo already sits in vaultDir (or writes plain files if
        that directory is not a git repo at all). Set it, and you also need a
        push credential in environmentFile.
      '';
    };

    branch = lib.mkOption {
      type = lib.types.str;
      default = "master";
      description = "Branch to sync and push.";
    };

    allowedUsers = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "alice" ];
      description = ''
        When non-empty, every tool call must carry an X-Auth-User header
        matching one of these, re-checked per call rather than only at an
        authenticating proxy. Empty means no check — fine on a machine only
        you can reach, wrong on anything exposed.
      '';
    };

    host = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = "Bind address. The default keeps it on the loopback; a container serving other machines wants 0.0.0.0.";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8000;
      description = "Port for the streamable-HTTP server (/mcp, GET /health, POST /hooks/gitlab).";
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Open the port in the host firewall.";
    };

    pollSeconds = lib.mkOption {
      type = lib.types.int;
      default = 900;
      description = "Backstop git sync interval. 0 disables it, leaving the webhook as the only sync path.";
    };

    gitName = lib.mkOption {
      type = lib.types.str;
      default = "GTD MCP";
      description = "Commit author name.";
    };

    gitEmail = lib.mkOption {
      type = lib.types.str;
      default = "gtd-mcp@noreply";
      description = "Commit author email.";
    };

    environmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "/var/lib/secrets/gtd-mcp.env";
      description = ''
        Path to a mode-0600 file of KEY=value lines, seeded out of band and
        never in git or an image. This is where GTD_PUSH_TOKEN and
        GTD_WEBHOOK_SECRET belong. A local-only server needs no such file,
        which is why the default is null.
      '';
    };

    requiredSecrets = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = lib.optionals (cfg.remoteUrl != null) [ "GTD_PUSH_TOKEN" ];
      defaultText = lib.literalExpression ''[ "GTD_PUSH_TOKEN" ] when remoteUrl is set, otherwise [ ]'';
      description = ''
        Environment variables the unit refuses to start without. Coming up
        insecure is a worse failure than not coming up, so a value that is
        empty or still "CHANGEME" fails the unit outright.
      '';
    };

    requiredUnits = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "cattle-state-preflight.service" ];
      description = ''
        Extra systemd units this service must wait for and depend on — a
        state-volume preflight, a mount unit, a VPN. Empty on an ordinary
        host, which is what makes this module portable.
      '';
    };

    extraEnvironment = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      description = "Extra environment variables for the unit, for settings this module has no option for.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.requiredSecrets == [ ] || cfg.environmentFile != null;
        message = "services.gtd-mcp: requiredSecrets is non-empty but environmentFile is null, so those values can never be supplied.";
      }
    ];

    users.groups.gtd-mcp = { };
    users.users.gtd-mcp = {
      isSystemUser = true;
      group = "gtd-mcp";
      home = cfg.stateDir;
      createHome = false;
    };

    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [ cfg.port ];

    # ReadWritePaths must exist before the unit's mount namespace is built,
    # which is before any ExecStartPre ("+" or not) — so tmpfiles, at boot.
    systemd.tmpfiles.rules = [
      "d ${cfg.stateDir} 0750 gtd-mcp gtd-mcp -"
      "d ${cfg.vaultDir} 0750 gtd-mcp gtd-mcp -"
    ];

    systemd.services.gtd-mcp = {
      description = "GTD MCP server (streamable-HTTP, /mcp on :${toString cfg.port})";
      after = [ "network-online.target" ] ++ cfg.requiredUnits;
      wants = [ "network-online.target" ];
      requires = cfg.requiredUnits;
      wantedBy = [ "multi-user.target" ];

      path = [ pkgs.git ];

      # Non-secret configuration only. GTD_PUSH_TOKEN and GTD_WEBHOOK_SECRET
      # come from environmentFile — never set here, never in git or an image.
      environment = {
        HOME = cfg.stateDir;
        GTD_VAULT_DIR = cfg.vaultDir;
        GTD_MCP_HOST = cfg.host;
        GTD_MCP_PORT = toString cfg.port;
        GTD_POLL_SECONDS = toString cfg.pollSeconds;
        GTD_GIT_NAME = cfg.gitName;
        GTD_GIT_EMAIL = cfg.gitEmail;
        GTD_BRANCH = cfg.branch;
      }
      // lib.optionalAttrs (cfg.remoteUrl != null) { GTD_REMOTE_URL = cfg.remoteUrl; }
      // lib.optionalAttrs (cfg.allowedUsers != [ ]) {
        GTD_ALLOWED_USERS = lib.concatStringsSep "," cfg.allowedUsers;
      }
      // cfg.extraEnvironment;

      serviceConfig = {
        Type = "simple";
        User = "gtd-mcp";
        Group = "gtd-mcp";

        # A missing file fails the unit outright, which is the "refuses to
        # start" contract for free.
        EnvironmentFile = lib.mkIf (cfg.environmentFile != null) cfg.environmentFile;

        ExecStartPre = [ "${gitConfigScript}" ]
          ++ lib.optional (cfg.requiredSecrets != [ ]) "${checkSecretsScript}";
        ExecStart = "${cfg.package}/bin/gtd-mcp serve";

        Restart = "on-failure";
        RestartSec = 5;

        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        NoNewPrivileges = true;
        ReadWritePaths = [ cfg.stateDir cfg.vaultDir ];
        CapabilityBoundingSet = "";
        RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];

        # An allow-list group, not `~@privileged`: subtracting @privileged
        # silently drops @chown as well, and the default action is a
        # SIGSYS kill. EPERM instead — git and python need nothing from
        # @privileged at runtime, but a refusal beats a kill if that
        # reasoning is ever wrong.
        SystemCallFilter = [ "@system-service" ];
        SystemCallErrorNumber = "EPERM";
      };
    };
  };
}
