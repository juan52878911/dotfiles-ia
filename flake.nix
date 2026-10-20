{
  description = "Configuración de IA de Juan: Claude Code (hooks, agentes y settings) como módulo de home-manager";

  outputs = { self, ... }: {
    # En los dotfiles: imports = [ ia-config.homeManagerModules.default ]; programs.ia-config.enable = true;
    homeManagerModules.default = import ./nix/modulo.nix self;
  };
}
