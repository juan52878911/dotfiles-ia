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

  options.programs.dotfiles-ia.tokeneconomizer = {
    enable = lib.mkEnableOption "TokenEconomizer (plugin de Claude Code que aprende de las sesiones de cada repo)";
    src = lib.mkOption {
      type = lib.types.path;
      description = "Fuente del plugin (un input del flake con flake = false, p. ej. un fork de microsoft/TokenEconomizer).";
    };
    configDirs = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "~/.local/state/ws/claude/trabajo" ];
      description = "Otros CLAUDE_CONFIG_DIR (cuentas) donde activarlo además de ~/.claude.";
    };
    claude = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = "${config.home.homeDirectory}/.local/bin/claude";
      description = "Binario de claude para instalarlo y actualizarlo al activar; null deja solo los settings.";
    };
    settings = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      example = { microskills = "on"; toolprune = "propose"; loopguard = "nudge"; };
      description = "Claves de ~/.tokeneconomizer/config.json (global). Se fusionan: `te config set --global` sigue valiendo.";
    };
    pi = lib.mkOption {
      type = lib.types.bool;
      default = cfg.pi.enable;
      description = "Instalarlo también como paquete de Pi.";
    };
  };

  options.programs.dotfiles-ia.diseno = {
    enable = lib.mkEnableOption "diseno-studio (plugin de Claude Code de sistema de diseño, con su binario Rust compilado por Nix)";
    package = lib.mkOption {
      type = lib.types.package;
      description = "El plugin ya empaquetado: `diseno-studio.packages.<sistema>.plugin` del flake de diseno-studio (trae bin/<so>-<arq>/diseno).";
    };
    configDirs = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "~/.local/state/ws/claude/trabajo" ];
      description = "Otros CLAUDE_CONFIG_DIR (cuentas) donde activarlo además de ~/.claude.";
    };
    claude = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = "${config.home.homeDirectory}/.local/bin/claude";
      description = "Binario de claude para instalarlo y actualizarlo al activar; null deja solo los settings.";
    };
    webModerna = {
      enable = lib.mkEnableOption ''
        web-moderna: las guías de Modern Web Guidance (equipo de Chrome) como plugin local de Claude Code,
        con una skill que las busca en los ficheros (sin npx @latest, sin red y sin telemetría)'';
      src = lib.mkOption {
        type = lib.types.path;
        description = "Fuente de las guías: un input del flake con flake = false, fijado a un commit de GoogleChrome/modern-web-guidance.";
      };
    };
  };

  config = lib.mkIf cfg.enable (lib.mkMerge [
    {
      home.file = {
        ".claude/agents".source = enlace "claude/agents";
        ".claude/hooks/damage-control.py".source = enlace "claude/hooks/damage-control.py";
        ".claude/hooks/no-cat-grande.py".source = enlace "claude/hooks/no-cat-grande.py";
        ".claude/hooks/chrono-primero.py".source = enlace "claude/hooks/chrono-primero.py";
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

    (lib.mkIf cfg.tokeneconomizer.enable (
      let
        te = cfg.tokeneconomizer;
        ruta = "${config.home.homeDirectory}/.local/share/TokenEconomizer";   # estable entre versiones del store
        dirs = [ "${config.home.homeDirectory}/.claude" ] ++ te.configDirs;
        ajustes = pkgs.writeText "tokeneconomizer-config.json" (builtins.toJSON te.settings);
      in
      {
        home.file.".local/share/TokenEconomizer".source = te.src;

        # Claude Code copia el plugin a su caché: los settings declaran el marketplace en `ruta` y el
        # `claude plugin update` recoge la versión nueva tras cambiar el input.
        home.activation.dotfilesIaTokeneconomizer = lib.hm.dag.entryAfter [ "linkGeneration" "dotfilesIaClaude" ] ''
          run ${pkgs.python3}/bin/python3 ${origen}/claude/activar-plugin.py tokeneconomizer tokeneconomizer \
            ${ruta} ${lib.escapeShellArgs dirs} ${lib.optionalString (te.claude != null) "--claude ${te.claude}"} || true
          run ${pkgs.python3}/bin/python3 -c '
          import json, os, sys
          p = os.path.expanduser("~/.tokeneconomizer/config.json")
          try: d = json.load(open(p))
          except (OSError, ValueError): d = {}
          n = dict(d, **json.load(open(sys.argv[1])))
          if n != d:
              os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
              json.dump(n, open(p, "w"), indent=2); print("tokeneconomizer: config.json actualizado")
          ' ${ajustes}
          ${lib.optionalString te.pi ''
            if [ -x ${config.home.homeDirectory}/.local/bin/pi ] && ! ${config.home.homeDirectory}/.local/bin/pi list 2>/dev/null | grep -qF ${ruta}; then
              run ${config.home.homeDirectory}/.local/bin/pi install ${ruta} || true
            fi
          ''}
        '';
      }
    ))

    (lib.mkIf cfg.diseno.enable (
      let
        ds = cfg.diseno;
        ruta = "${config.home.homeDirectory}/.local/share/diseno-studio";   # estable entre versiones del store
        dirs = [ "${config.home.homeDirectory}/.claude" ] ++ ds.configDirs;
      in
      {
        home.file.".local/share/diseno-studio".source = ds.package;

        # Mismo esquema que tokeneconomizer: el marketplace local apunta a `ruta`, y tras un rebuild
        # `claude plugin update` recoge el plugin nuevo (con el binario recién compilado).
        home.activation.dotfilesIaDiseno = lib.hm.dag.entryAfter [ "linkGeneration" "dotfilesIaClaude" ] ''
          run ${pkgs.python3}/bin/python3 ${origen}/claude/activar-plugin.py diseno-studio diseno-studio \
            ${ruta} ${lib.escapeShellArgs dirs} ${lib.optionalString (ds.claude != null) "--claude ${ds.claude}"} || true
        '';
      }
    ))

    (lib.mkIf cfg.diseno.webModerna.enable (
      let
        wm = cfg.diseno.webModerna;
        ruta = "${config.home.homeDirectory}/.local/share/web-moderna";
        dirs = [ "${config.home.homeDirectory}/.claude" ] ++ cfg.diseno.configDirs;
        version = "1.0.0-${builtins.substring 0 7 (wm.src.rev or "local")}";
        # Plugin propio: las 187 guías del upstream + nuestra skill (claude/web-moderna/SKILL.md),
        # que las busca con Glob/Grep. La skill del upstream llama a `npx -y modern-web-guidance@latest`.
        plugin = pkgs.runCommand "web-moderna-${version}" { } ''
          mkdir -p $out/.claude-plugin $out/skills/web-moderna
          cp -R ${wm.src}/skills/modern-web-guidance/guides $out/skills/web-moderna/guides
          cp ${self}/claude/web-moderna/SKILL.md $out/skills/web-moderna/SKILL.md
          cp ${wm.src}/LICENSE $out/LICENSE
          cat > $out/README.md <<'EOF'
        # web-moderna

        Las 187 guías de Modern Web Guidance (equipo de Chrome, con Edge, Firefox y MDN) como plugin local de
        Claude Code. La skill `web-moderna` las busca con Glob y Grep en `skills/web-moderna/guides/` antes de
        implementar UI web, para usar las APIs actuales de HTML, CSS y JS en lugar de librerías o patrones
        antiguos. No usa npx, ni red, ni telemetría: las guías vienen de un commit fijado en los dotfiles.
        Lo empaqueta dotfiles-ia (`programs.dotfiles-ia.diseno.webModerna`); las licencias están en LICENSE y NOTICE.
        EOF
          cat > $out/NOTICE <<'EOF'
        Guías de GoogleChrome/modern-web-guidance (https://github.com/GoogleChrome/modern-web-guidance):
        guías bajo CC-BY 4.0 y código bajo Apache-2.0 (ver LICENSE), sin modificar. La skill es de dotfiles-ia.
        EOF
          cat > $out/.claude-plugin/plugin.json <<'EOF'
        {"name": "web-moderna", "version": "${version}",
         "description": "Guías de la plataforma web moderna (Modern Web Guidance de Chrome) buscadas en local, sin red ni telemetría",
         "author": {"name": "Juan Bedoya"}, "license": "Apache-2.0"}
        EOF
          cat > $out/.claude-plugin/marketplace.json <<'EOF'
        {"name": "web-moderna", "owner": {"name": "Juan Bedoya"},
         "description": "Guías de Modern Web Guidance (Chrome) empaquetadas por dotfiles-ia para buscarlas en local",
         "plugins": [{"name": "web-moderna", "source": "./", "version": "${version}",
                      "description": "Guías de la plataforma web moderna, en local"}]}
        EOF
        '';
      in
      {
        home.file.".local/share/web-moderna".source = plugin;
        home.activation.dotfilesIaWebModerna = lib.hm.dag.entryAfter [ "linkGeneration" "dotfilesIaClaude" ] ''
          run ${pkgs.python3}/bin/python3 ${origen}/claude/activar-plugin.py web-moderna web-moderna \
            ${ruta} ${lib.escapeShellArgs dirs} ${lib.optionalString (cfg.diseno.claude != null) "--claude ${cfg.diseno.claude}"} || true
        '';
      }
    ))

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
