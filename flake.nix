{
  description = "Arc Payables — Arc Canteen Hackathon dev environment";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      supportedSystems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forEachSupportedSystem = f: nixpkgs.lib.genAttrs supportedSystems (system: f {
        pkgs = import nixpkgs { inherit system; };
      });
    in {
      devShells = forEachSupportedSystem ({ pkgs }: {
        default = pkgs.mkShell {
          packages = with pkgs; [
            uv
            python312
            nodejs_22
            pnpm
            foundry
            solc
            curl
            jq
            git
          ];

          shellHook = ''
            if [ -d ".venv/bin" ]; then
              export PATH="$PWD/.venv/bin:$PATH"
            fi
            if [ -t 1 ]; then
              echo "Arc Payables Dev Shell ready."
              echo "• uv $(uv --version 2>/dev/null | awk '{print $2}')"
              echo "• node $(node --version 2>/dev/null)"
              echo "• forge $(forge --version 2>/dev/null | awk '{print $2}')"
              echo "• solc $(solc --version 2>/dev/null | awk '/Version:/ {print $2}')"
              echo "Run 'uv sync' to install project-local tools (like arc-canteen) into .venv."
            fi
          '';
        };
      });
    };
}
