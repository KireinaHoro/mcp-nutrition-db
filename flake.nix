{
  description = "Private MCP service for a conversational nutrition log";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      packageFor = system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python = pkgs.python313;
        in
        python.pkgs.buildPythonApplication {
          pname = "mcp-nutrition-db";
          version = "0.1.0";
          pyproject = true;
          src = self;

          build-system = [ python.pkgs.setuptools ];
          dependencies = with python.pkgs; [ mcp pydantic ];

          nativeCheckInputs = with python.pkgs; [ pytestCheckHook ];
          pytestFlags = [ "tests" ];
          pythonImportsCheck = [ "mcp_nutrition_db" ];

          meta = {
            description = "Private MCP service for a conversational nutrition log";
            license = pkgs.lib.licenses.mit;
            mainProgram = "mcp-nutrition-db";
            platforms = systems;
          };
        };
    in
    {
      packages = forAllSystems (system: {
        default = packageFor system;
      });

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/mcp-nutrition-db";
          meta.description = "Run the nutrition MCP server";
        };
      });

      checks = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          package = self.packages.${system}.default;
          nixos-module = (nixpkgs.lib.nixosSystem {
            inherit system;
            modules = [
              self.nixosModules.default
              {
                boot.loader.grub.enable = false;
                fileSystems."/" = {
                  device = "none";
                  fsType = "tmpfs";
                };
                services.mcp-nutrition-db.enable = true;
                services.mcp-nutrition-db.backup.enable = true;
                system.stateVersion = "25.05";
              }
            ];
          }).config.system.build.toplevel;
        });

      devShells = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python = pkgs.python313;
        in
        {
          default = pkgs.mkShell {
            packages = [
              (python.withPackages (ps: with ps; [ mcp pydantic pytest mypy ruff ]))
              pkgs.sqlite
            ];
          };
        });

      nixosModules.default = import ./nix/module.nix { inherit self; };
    };
}
