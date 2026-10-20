{
  description = "Configuración de IA de Juan: Claude Code (hooks, agentes y settings) como módulo de home-manager";

  outputs = { self, ... }: {
    # En los dotfiles: imports = [ dotfiles-ia.homeManagerModules.default ]; programs.dotfiles-ia.enable = true;
    homeManagerModules.default = import ./nix/modulo.nix self;
  };
}
