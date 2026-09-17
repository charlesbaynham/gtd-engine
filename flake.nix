{
  description = "gtd-mcp: a structure-aware MCP server on a markdown GTD vault";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

    # Used only by the Proxmox LXC template outputs at the bottom of this
    # file. If you have no Proxmox server, ignore them and nothing here
    # changes: `packages.*.gtd-mcp` and `nixosModules.gtd-mcp` never touch
    # this input.
    cattle.url = "github:charlesbaynham/nix-proxmox-cattle";
  };

  outputs = { self, nixpkgs, cattle }:
    let
      lib = nixpkgs.lib;
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f: lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      # Built against whatever nixpkgs the *importing* host uses, so the module
      # is portable. `mcp` comes from nixpkgs' own pin (1.26.0 on nixos-26.05)
      # — write against that, never a newer one from pip: the SDK moves fast
      # enough that a floating version is a reliable source of silent breakage.
      gtdPackages = pkgs:
        let py = pkgs.python3Packages; in
        rec {
          gtd-ci = py.buildPythonPackage {
            pname = "gtd-ci";
            version = "0.1.0";
            pyproject = true;
            # Package source only — never ./., or the vault's markdown would
            # ride along into the store as part of a "code" derivation.
            src = ./.gtd/ci;
            build-system = [ py.setuptools ];
          };

          gtd-mcp = py.buildPythonApplication {
            pname = "gtd-mcp";
            version = "0.1.0";
            pyproject = true;
            src = ./.gtd/mcp;
            build-system = [ py.setuptools ];
            dependencies = [ gtd-ci py.mcp ];
            meta.mainProgram = "gtd-mcp";
          };
        };

      # The portable module: works on any NixOS machine — bare metal, a VM, a
      # systemd-nspawn container, an LXC you manage by hand.
      gtdMcpModule = { pkgs, ... }: {
        imports = [ ./.gtd/nix/gtd-mcp.nix ];
        services.gtd-mcp.package = lib.mkDefault (gtdPackages pkgs).gtd-mcp;
      };

      # Your own settings, if you made them. Without this file the template
      # below still builds; it just produces a local-only server.
      deploymentModules = lib.optional (builtins.pathExists ./.gtd/nix/deployment.nix)
        ./.gtd/nix/deployment.nix;
    in
    lib.recursiveUpdate
      {
        packages = forAllSystems (pkgs: {
          inherit (gtdPackages pkgs) gtd-ci gtd-mcp;
          default = (gtdPackages pkgs).gtd-mcp;
        });

        nixosModules.gtd-mcp = gtdMcpModule;
        nixosModules.default = gtdMcpModule;
      }
      # Proxmox LXC template: `nix build .#proxmoxLxcTemplate`, or the
      # lxc-template GitHub workflow once you set the BUILD_LXC_TEMPLATE repo
      # variable. Harmless to leave alone if you have no hypervisor.
      (cattle.lib.mkTemplate {
        inherit nixpkgs;
        name = "gtd-mcp";
        stateDir = "/data";
        modules = [
          self.nixosModules.gtd-mcp
          ./.gtd/nix/cattle.nix
          { services.gtd-mcp.enable = true; }
        ] ++ deploymentModules;
      });
}
