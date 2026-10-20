# Módulo de home-manager: enlaza los agentes y hooks de Claude Code y fusiona settings-base.json en
# ~/.claude/settings.json. settings.json NO se enlaza: Claude Code, Nod y Orbit escriben en él.
self: { config, lib, pkgs, ... }:
let
  cfg = config.programs.dotfiles-ia;
  origen = if cfg.checkout != null then cfg.checkout else "${self}";
  enlace = ruta:
    if cfg.checkout != null
    then config.lib.file.mkOutOfStoreSymlink "${cfg.checkout}/${ruta}"
    else "${self}/${ruta}";
in
{
  options.programs.dotfiles-ia = {
    enable = lib.mkEnableOption "la configuración de IA de dotfiles-ia (Claude Code)";
    checkout = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "/Users/juan/Documents/GitHub/dotfiles-ia";
      description = ''
        Clon local de dotfiles-ia. Si se da, los ficheros se enlazan a él y los cambios se ven al
        instante, sin rebuild (y lo que Claude Code escriba en ~/.claude/agents aterriza en el repo).
        Si no, se enlazan a la copia del store.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    home.file = {
      ".claude/agents".source = enlace "claude/agents";
      ".claude/hooks/damage-control.py".source = enlace "claude/hooks/damage-control.py";
      ".claude/hooks/no-cat-grande.py".source = enlace "claude/hooks/no-cat-grande.py";
    };

    home.activation.dotfilesIaClaude = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
      run ${pkgs.python3}/bin/python3 ${origen}/claude/fusionar-settings.py
    '';
  };
}
