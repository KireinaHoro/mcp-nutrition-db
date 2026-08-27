{ self }:
{ config, lib, pkgs, ... }:

let
  cfg = config.services.mcp-nutrition-db;
in
{
  options.services.mcp-nutrition-db = {
    enable = lib.mkEnableOption "the private MCP nutrition database service";

    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      defaultText = lib.literalExpression "inputs.mcp-nutrition-db.packages.${pkgs.system}.default";
      description = "Package providing the mcp-nutrition-db executable.";
    };

    listenAddress = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = "Loopback address used for Streamable HTTP.";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8787;
      description = "Loopback TCP port used for Streamable HTTP.";
    };

    defaultTimezone = lib.mkOption {
      type = lib.types.str;
      default = "Europe/Zurich";
      description = "IANA timezone used when a tool call omits one.";
    };

    logLevel = lib.mkOption {
      type = lib.types.enum [ "debug" "info" "warning" "error" ];
      default = "info";
      description = "Minimum level for redacted structured application logs.";
    };

    stateDirectory = lib.mkOption {
      type = lib.types.str;
      default = "mcp-nutrition-db";
      description = "Name of the persistent directory below /var/lib.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = lib.elem cfg.listenAddress [ "127.0.0.1" "::1" "localhost" ];
        message = "services.mcp-nutrition-db.listenAddress must be a loopback address";
      }
    ];

    systemd.services.mcp-nutrition-db = {
      description = "Private MCP nutrition database";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" ];

      serviceConfig = {
        Type = "simple";
        DynamicUser = true;
        StateDirectory = cfg.stateDirectory;
        StateDirectoryMode = "0700";
        ExecStart = lib.escapeShellArgs [
          "${cfg.package}/bin/mcp-nutrition-db"
          "serve"
          "--database"
          "/var/lib/${cfg.stateDirectory}/nutrition.sqlite3"
          "--host"
          cfg.listenAddress
          "--port"
          (toString cfg.port)
          "--timezone"
          cfg.defaultTimezone
          "--log-level"
          cfg.logLevel
        ];
        Restart = "on-failure";
        RestartSec = 2;

        NoNewPrivileges = true;
        PrivateDevices = true;
        PrivateTmp = true;
        ProtectClock = true;
        ProtectControlGroups = true;
        ProtectHome = true;
        ProtectHostname = true;
        ProtectKernelLogs = true;
        ProtectKernelModules = true;
        ProtectKernelTunables = true;
        ProtectSystem = "strict";
        RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
        RestrictNamespaces = true;
        RestrictRealtime = true;
        SystemCallArchitectures = "native";
        UMask = "0077";
      };
    };
  };
}
