{
  description = "Private MCP service for a conversational nutrition log";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      usdaDatabaseFor = system:
        nixpkgs.legacyPackages.${system}.callPackage ./nix/usda-database.nix { src = self; };
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
          dependencies = with python.pkgs; [ mcp pydantic garminconnect ];

          nativeCheckInputs = with python.pkgs; [ pytestCheckHook ];
          pytestFlags = [ "tests" ];
          pythonImportsCheck = [ "mcp_nutrition_db" ];
          makeWrapperArgs = [
            "--set-default MCP_NUTRITION_USDA_DATABASE ${usdaDatabaseFor system}/share/mcp-nutrition-db/usda.sqlite3"
          ];

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
        usda-database = usdaDatabaseFor system;
      });

      apps = forAllSystems (system: {
        garmin-login = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/garmin-login";
          meta.description = "Generate a private Garmin session for sops";
        };
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
          garmin-shared-state = import ./nix/garmin-service-test.nix { inherit pkgs self system; };
          quality = pkgs.runCommand "mcp-nutrition-db-quality" {
            nativeBuildInputs = [
              (pkgs.python313.withPackages (ps: with ps; [ mcp pydantic mypy ruff ]))
            ];
          } ''
            cp -r ${self} source
            chmod -R u+w source
            cd source
            export PYTHONPATH=src
            ruff format --check src tests
            ruff check src tests
            mypy src
            touch "$out"
          '';
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
              (python.withPackages (ps: with ps; [ mcp pydantic garminconnect pytest mypy ruff ]))
              pkgs.sqlite
            ];
          };
        });

      nixosModules.default = import ./nix/module.nix { inherit self; };
    };
}
