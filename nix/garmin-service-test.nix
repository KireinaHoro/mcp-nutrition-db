{ pkgs, self, system }:
let
  app = self.packages.${system}.default;
  probe = pkgs.writeShellScript "garmin-state-access-probe" ''
    set -eu
    test -w /var/lib/mcp-nutrition-db/nutrition.sqlite3
    mkdir -p /var/lib/mcp-nutrition-db/garmin
    test -r "$CREDENTIALS_DIRECTORY/garmin-session"
    printf synthetic > /var/lib/mcp-nutrition-db/garmin/probe-token
    ${app}/bin/mcp-nutrition-db garmin \
      --database /var/lib/mcp-nutrition-db/nutrition.sqlite3 \
      --state-directory /var/lib/mcp-nutrition-db/garmin status
  '';
in
pkgs.testers.runNixOSTest {
  name = "nutrition-garmin-shared-state";
  nodes.machine = { lib, ... }: {
    imports = [ self.nixosModules.default ];
    services.mcp-nutrition-db = {
      enable = true;
      garmin = {
        enable = true;
        credentialsFile = "/run/synthetic-garmin-session";
      };
    };
    systemd.services.mcp-nutrition-db-garmin.serviceConfig.ExecStart = lib.mkForce probe;
    systemd.timers.mcp-nutrition-db-garmin.wantedBy = lib.mkForce [];
    systemd.timers.mcp-nutrition-db-garmin-weekly.wantedBy = lib.mkForce [];
  };
  testScript = ''
    machine.start(allow_reboot=True)
    machine.wait_for_unit("mcp-nutrition-db.service")
    machine.succeed("install -m 0600 /dev/null /run/synthetic-garmin-session")
    machine.succeed("systemctl start mcp-nutrition-db-garmin.service")
    machine.succeed("systemctl restart mcp-nutrition-db.service")
    machine.wait_for_unit("mcp-nutrition-db.service")
    machine.succeed("systemctl start mcp-nutrition-db-garmin.service")
    machine.succeed("test -f /var/lib/mcp-nutrition-db/garmin/probe-token")
    machine.reboot()
    machine.wait_for_unit("mcp-nutrition-db.service")
    machine.succeed("test -f /var/lib/mcp-nutrition-db/garmin/probe-token")
    machine.fail("test -f /run/synthetic-garmin-session")
    machine.succeed("install -m 0600 /dev/null /run/synthetic-garmin-session")
    machine.succeed("systemctl start mcp-nutrition-db-garmin.service")
    machine.succeed("curl --fail http://127.0.0.1:8787/healthz")
    machine.succeed("systemctl show mcp-nutrition-db-garmin.service -p User --value | grep -x mcp-nutrition-db")
  '';
}
