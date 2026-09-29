{ self }:
{ config, lib, pkgs, ... }:

let
  cfg = config.services.mcp-nutrition-db;
  healthHost = if cfg.listenAddress == "::1" then "[::1]" else cfg.listenAddress;
  readinessCheck = pkgs.writeShellApplication {
    name = "mcp-nutrition-db-readiness-check";
    runtimeInputs = [ pkgs.curl ];
    text = ''
      curl --fail --silent --show-error \
        --retry 30 --retry-delay 1 --retry-connrefused \
        "http://${healthHost}:${toString cfg.port}/healthz" >/dev/null
    '';
  };
  backupScript = pkgs.writeShellApplication {
    name = "mcp-nutrition-db-incremental-backup";
    runtimeInputs = [ cfg.package pkgs.coreutils pkgs.procps pkgs.rdiff-backup ];
    text = ''
      snapshot_dir=${lib.escapeShellArg "${cfg.backup.directory}/snapshot"}
      repository=${lib.escapeShellArg "${cfg.backup.directory}/increments"}

      install -d -m 0700 "$snapshot_dir"
      mcp-nutrition-db backup \
        --database ${lib.escapeShellArg "/var/lib/${cfg.stateDirectory}/nutrition.sqlite3"} \
        --output "$snapshot_dir/nutrition.sqlite3"
      rdiff-backup --api-version 201 backup \
        --create-full-path "$snapshot_dir" "$repository"
      retention_status=0
      rdiff-backup --api-version 201 remove increments \
        --older-than ${lib.escapeShellArg cfg.backup.retention} "$repository" \
        || retention_status=$?
      # rdiff-backup exits 2 when pruning emits a warning, including the
      # expected first-run case where no increment is old enough to remove.
      if [[ "$retention_status" -ne 0 && "$retention_status" -ne 2 ]]; then
        exit "$retention_status"
      fi
    '';
  };
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

    usdaDatabase = lib.mkOption {
      type = lib.types.path;
      default = "${self.packages.${pkgs.stdenv.hostPlatform.system}.usda-database}/share/mcp-nutrition-db/usda.sqlite3";
      description = "Read-only USDA reference database. All USDA lookups are offline; updates require a new pinned dataset.";
    };

    stateDirectory = lib.mkOption {
      type = lib.types.str;
      default = "mcp-nutrition-db";
      description = "Name of the persistent directory below /var/lib.";
    };

    garmin = {
      enable = lib.mkEnableOption "scheduled Garmin weight and activity imports";
      credentialsFile = lib.mkOption {
        type = lib.types.strMatching "^/.*";
        description = "Runtime path to the sops-decrypted Garmin login bundle. Never use a Nix store path or inline secret. A new session_id seeds refreshed runtime tokens once.";
      };
    };

    backup = {
      enable = lib.mkEnableOption "weekly local incremental nutrition database backups";

      directory = lib.mkOption {
        type = lib.types.strMatching "^/.*";
        default = "/var/backup/mcp-nutrition-db";
        description = "Root-only directory containing the latest SQLite snapshot and rdiff history.";
      };

      onCalendar = lib.mkOption {
        type = lib.types.str;
        default = "weekly";
        description = "systemd OnCalendar expression for database backups.";
      };

      randomizedDelaySec = lib.mkOption {
        type = lib.types.str;
        default = "1h";
        description = "Maximum randomized delay applied to each scheduled backup.";
      };

      retention = lib.mkOption {
        type = lib.types.str;
        default = "26W";
        description = "rdiff-backup age expression for retained incremental history.";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = lib.elem cfg.listenAddress [ "127.0.0.1" "::1" "localhost" ];
        message = "services.mcp-nutrition-db.listenAddress must be a loopback address";
      }
    ] ++ lib.optional cfg.garmin.enable {
      assertion = !(lib.hasPrefix "/nix/store/" cfg.garmin.credentialsFile);
      message = "Garmin credentialsFile must point to a runtime secret outside the Nix store";
    };

    systemd.services.mcp-nutrition-db = {
      description = "Private MCP nutrition database";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" ];

      environment.MCP_NUTRITION_USDA_DATABASE = toString cfg.usdaDatabase;

      serviceConfig = {
        Type = "simple";
        DynamicUser = true;
        User = "mcp-nutrition-db";
        Group = "mcp-nutrition-db";
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
        ExecStartPost = lib.getExe readinessCheck;
        Restart = "on-failure";
        RestartSec = 2;
        TimeoutStartSec = 45;

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

    systemd.services.mcp-nutrition-db-garmin = lib.mkIf cfg.garmin.enable {
      description = "Garmin nutrition sync";
      after = [ "network-online.target" "mcp-nutrition-db.service" ];
      wants = [ "network-online.target" ];
      serviceConfig = {
        Type = "oneshot";
        DynamicUser = true;
        User = "mcp-nutrition-db";
        Group = "mcp-nutrition-db";
        StateDirectory = cfg.stateDirectory;
        StateDirectoryMode = "0700";
        LoadCredential = [ "garmin-session:${cfg.garmin.credentialsFile}" ];
        ExecStart = lib.escapeShellArgs [
          "${cfg.package}/bin/mcp-nutrition-db" "garmin"
          "--database" "/var/lib/${cfg.stateDirectory}/nutrition.sqlite3"
          "--state-directory" "/var/lib/${cfg.stateDirectory}/garmin"
          "--credentials-file" "%d/garmin-session" "sync"
        ];
        TimeoutStartSec = "25min";
        UMask = "0077";
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectHome = true;
        ProtectSystem = "strict";
        RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
      };
    };
    systemd.services.mcp-nutrition-db-garmin-weekly = lib.mkIf cfg.garmin.enable {
      description = "Garmin 90-day reconciliation scan";
      after = [ "network-online.target" "mcp-nutrition-db.service" ];
      wants = [ "network-online.target" ];
      serviceConfig = config.systemd.services.mcp-nutrition-db-garmin.serviceConfig // {
        ExecStart = lib.escapeShellArgs [
          "${cfg.package}/bin/mcp-nutrition-db" "garmin"
          "--database" "/var/lib/${cfg.stateDirectory}/nutrition.sqlite3"
          "--state-directory" "/var/lib/${cfg.stateDirectory}/garmin"
          "--credentials-file" "%d/garmin-session" "sync" "--weekly"
        ];
      };
    };
    systemd.timers.mcp-nutrition-db-garmin = lib.mkIf cfg.garmin.enable {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = "*-*-* *:00,30:00";
        Persistent = true;
        RandomizedDelaySec = "2min";
        AccuracySec = "1s";
      };
    };
    systemd.timers.mcp-nutrition-db-garmin-weekly = lib.mkIf cfg.garmin.enable {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = "Sun *-*-* 04:15:00";
        Persistent = true;
        RandomizedDelaySec = "2min";
        AccuracySec = "1s";
      };
    };

    systemd.tmpfiles.rules = lib.mkIf cfg.backup.enable [
      "d ${cfg.backup.directory} 0700 root root -"
    ];

    systemd.services.mcp-nutrition-db-backup = lib.mkIf cfg.backup.enable {
      description = "Incremental backup of the MCP nutrition database";
      requires = [ "mcp-nutrition-db.service" ];
      after = [ "mcp-nutrition-db.service" ];
      unitConfig.ConditionPathExists = "/var/lib/${cfg.stateDirectory}/nutrition.sqlite3";

      serviceConfig = {
        Type = "oneshot";
        User = "root";
        ExecStart = lib.getExe backupScript;
        UMask = "0077";

        CapabilityBoundingSet = [ "CAP_DAC_READ_SEARCH" ];
        LockPersonality = true;
        NoNewPrivileges = true;
        PrivateDevices = true;
        PrivateNetwork = true;
        PrivateTmp = true;
        ProtectClock = true;
        ProtectControlGroups = true;
        ProtectHome = true;
        ProtectHostname = true;
        ProtectKernelLogs = true;
        ProtectKernelModules = true;
        ProtectKernelTunables = true;
        ProtectSystem = "strict";
        ReadWritePaths = [ cfg.backup.directory ];
        RestrictAddressFamilies = [ "AF_UNIX" ];
        RestrictNamespaces = true;
        RestrictRealtime = true;
        SystemCallArchitectures = "native";
      };
    };

    systemd.timers.mcp-nutrition-db-backup = lib.mkIf cfg.backup.enable {
      description = "Weekly incremental backup of the MCP nutrition database";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.backup.onCalendar;
        Persistent = true;
        RandomizedDelaySec = cfg.backup.randomizedDelaySec;
        Unit = "mcp-nutrition-db-backup.service";
      };
    };
  };
}
