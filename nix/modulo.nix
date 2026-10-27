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

  options.programs.dotfiles-ia.pi = {
    enable = lib.mkEnableOption "Pi (pi.dev) como segundo harness, con Node 22 propio y la extensión damage-control";
    version = lib.mkOption {
      type = lib.types.str;
      default = "1.3.0";
      description = "Versión de @earendil-works/pi-coding-agent que se instala en ~/.local/share/pi.";
    };
    llavero = lib.mkOption {
      type = lib.types.str;
      default = "minimax-coding-plan";
      description = ''
        Servicio del Llavero de macOS con la clave de MiniMax (Coding Plan). El envoltorio la exporta como
        MINIMAX_API_KEY si no está ya en el entorno. Guardarla: security add-generic-password -a "$USER" -s <servicio> -w
      '';
    };
  };

  options.programs.dotfiles-ia.chronoIa = {
    enable = lib.mkEnableOption ''
      un índice de chrono para las operaciones de IA (trabajos de delega, eventos de Nod y transcripciones
      de Claude Code): el comando `chrono-ia` y un agente de launchd que lo sincroniza'';
    binario = lib.mkOption {
      type = lib.types.str;
      default = "${config.home.homeDirectory}/.local/bin/chrono";
      description = "Binario de chrono (>= 0.7) que usan `chrono-ia` y la sincronización.";
    };
    db = lib.mkOption {
      type = lib.types.str;
      default = "${config.home.homeDirectory}/.local/state/chrono-ia/index.db";
      description = ''
        Índice. Las fuentes se añaden una vez a mano (quedan en el propio índice), p. ej.:
          chrono-ia add <state_dir de delega> --as delega
          chrono-ia add ~/.local/state/nod/events.jsonl --as nod
          chrono-ia add '~/.claude/projects/**/*.jsonl' --as jsonl --name claude --preset claude-sessions
      '';
    };
    intervalo = lib.mkOption {
      type = lib.types.int;
      default = 300;
      description = "Segundos entre sincronizaciones (un sync incremental tarda menos de un segundo).";
    };
  };

  config = lib.mkIf cfg.enable (lib.mkMerge [
    {
      home.file = {
        ".claude/agents".source = enlace "claude/agents";
        ".claude/hooks/damage-control.py".source = enlace "claude/hooks/damage-control.py";
        ".claude/hooks/no-cat-grande.py".source = enlace "claude/hooks/no-cat-grande.py";
        ".claude/hooks/delega-scan.py".source = enlace "claude/hooks/delega-scan.py";
        ".claude/hooks/delega-pr-gate.py".source = enlace "claude/hooks/delega-pr-gate.py";
        ".claude/hooks/modelo-orquestador.py".source = enlace "claude/hooks/modelo-orquestador.py";
      };

      home.activation.dotfilesIaClaude = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
        run ${pkgs.python3}/bin/python3 ${origen}/claude/fusionar-settings.py
      '';
    }

    (lib.mkIf cfg.chronoIa.enable {
      home.file.".local/bin/chrono-ia" = {
        executable = true;
        text = ''
          #!/bin/sh
          # chrono sobre el índice de operaciones de IA: chrono-ia delega, chrono-ia tail --source delega,nod …
          exec ${cfg.chronoIa.binario} --db ${cfg.chronoIa.db} "$@"
        '';
      };
      # Sin el sync, las consultas ven el índice de la última vez: Nod y las transcripciones crecen sin parar.
      launchd.agents.chrono-ia-sync = {
        enable = true;
        config = {
          ProgramArguments = [ cfg.chronoIa.binario "sync" "--db" cfg.chronoIa.db ];
          StartInterval = cfg.chronoIa.intervalo;
          RunAtLoad = true;
          Nice = 10;
          LowPriorityIO = true;
          StandardOutPath = "${config.home.homeDirectory}/Library/Logs/chrono-ia-sync.log";
          StandardErrorPath = "${config.home.homeDirectory}/Library/Logs/chrono-ia-sync.log";
        };
      };
    })

    (lib.mkIf cfg.pi.enable (
      let
        prefijo = "${config.home.homeDirectory}/.local/share/pi";
        cli = "${prefijo}/lib/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js";
        node = pkgs.nodejs_22;
      in
      {
        # Pi pide Node >= 22.19; el Node por defecto (nvm) se queda como está porque `claude` depende de él.
        home.file.".local/bin/pi" = {
          executable = true;
          text = ''
            #!/bin/sh
            if [ -z "$MINIMAX_API_KEY" ]; then
              MINIMAX_API_KEY=$(/usr/bin/security find-generic-password -s ${cfg.pi.llavero} -w 2>/dev/null) && export MINIMAX_API_KEY
            fi
            exec ${node}/bin/node ${cli} "$@"
          '';
        };
        home.file.".pi/agent/extensions/damage-control.ts".source = enlace "pi/extensions/damage-control.ts";

        home.activation.dotfilesIaPi = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
          actual=$(${node}/bin/node ${cli} --version 2>/dev/null || true)
          if [ "$actual" != "${cfg.pi.version}" ]; then
            run env PATH=${node}/bin:$PATH ${node}/bin/npm install -g --prefix ${prefijo} \
              @earendil-works/pi-coding-agent@${cfg.pi.version}
          fi
        '';
      }
    ))
  ]);
}
