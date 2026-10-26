"""Casos de los hooks delega-scan, delega-pr-gate y modelo-orquestador, con un repo y una config de prueba.
Usa el delega de este repo (delega/src) a través de un `delega` falso en PATH. Términos inventados."""
import json, os, subprocess, sys, tempfile, time

RAIZ = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
HOOKS = os.path.join(RAIZ, 'claude', 'hooks')
tmp = tempfile.mkdtemp(prefix='hooks-delega-')
binario = os.path.join(tmp, 'bin')
os.makedirs(binario)
with open(os.path.join(binario, 'delega'), 'w') as f:
    f.write(f'#!/bin/sh\nPYTHONPATH="{os.path.join(RAIZ, "delega", "src")}" exec "{sys.executable}" -m delega "$@"\n')
os.chmod(os.path.join(binario, 'delega'), 0o755)
terminos = os.path.join(tmp, 'prohibidos.txt')
open(terminos, 'w').write('# inventados\nZorbacorp\n')
estado = os.path.join(tmp, 'estado')
os.makedirs(estado)


def config(nombre, con_terminos=True):
    p = os.path.join(tmp, nombre)
    with open(p, 'w') as f:
        f.write(f'[general]\nstate_dir = "{estado}"\ndefault_profile = "w"\n[profiles.w]\nowner = "self"\n'
                f'repo_match = {{ git_remote = "example-org/" }}\n'
                + (f'forbidden_terms_file = "{terminos}"\n' if con_terminos else ''))
    return p


CON, SIN = config('con.toml'), config('sin.toml', False)
repo = os.path.join(tmp, 'repo')
g = lambda *a: subprocess.run(['git', '-C', repo, *a], capture_output=True, text=True, check=True).stdout.strip()
os.makedirs(repo)
g('init', '-q', '-b', 'main')
g('config', 'user.email', 't@example.com'); g('config', 'user.name', 'T')
g('remote', 'add', 'origin', 'git@github.com:example-org/app.git')
open(os.path.join(repo, 'a.txt'), 'w').write('hola\n'); g('add', '.'); g('commit', '-qm', 'base')
g('update-ref', 'refs/remotes/origin/main', 'HEAD')
g('checkout', '-qb', 'delega/limpia')
open(os.path.join(repo, 'b.txt'), 'w').write('limpio\n'); g('add', '.'); g('commit', '-qm', 'limpio')
g('checkout', '-qb', 'feature/sucia', 'main')
open(os.path.join(repo, 'c.txt'), 'w').write('datos de zorbacorp\n'); g('add', '.'); g('commit', '-qm', 'c')
g('checkout', '-q', 'delega/limpia')


def hook(nombre, cmd, cfg=CON, env=None, ev=None):
    e = dict(os.environ, PATH=binario + os.pathsep + os.environ['PATH'], DELEGA_CONFIG=cfg, DELEGA_STATE_DIR=estado)
    e.pop('DELEGA_OWNER', None); e.pop('DELEGA_CHANNEL', None)
    e.update(env or {})
    datos = ev or {'tool_name': 'Bash', 'tool_input': {'command': cmd}, 'cwd': repo}
    t0 = time.perf_counter()
    r = subprocess.run([sys.executable, os.path.join(HOOKS, nombre)], input=json.dumps(datos), capture_output=True,
                       text=True, env=e)
    return r, (time.perf_counter() - t0) * 1000


def auditar(**campos):
    with open(os.path.join(estado, 'audit.jsonl'), 'a') as f:
        f.write(json.dumps(dict({'ts': 'x', 'event': 'integrate'}, **campos)) + '\n')


sha_limpia = g('rev-parse', '--short', 'delega/limpia')
fallos = 0


def caso(nombre, obtenido, esperado, extra=''):
    global fallos
    ok = obtenido == esperado
    fallos += not ok
    print(('ok   ' if ok else 'FALLO'), f'{obtenido:9}', nombre, extra)


def decision(r):
    return 'bloquear' if r.returncode == 2 else 'pasar' if r.returncode == 0 else f'rc={r.returncode}'


# delega-scan
r, ms = hook('delega-scan.py', 'git status')
caso('scan: git status', decision(r), 'pasar', f'{ms:.0f} ms')
r, ms = hook('delega-scan.py', 'git commit -m "nada"', cfg=SIN)
caso('scan: sin términos', decision(r), 'pasar', f'{ms:.0f} ms')
caso('scan: sin términos < 300 ms', 'rápido' if ms < 300 else 'lento', 'rápido', f'{ms:.0f} ms')
r, _ = hook('delega-scan.py', 'git commit -m "nada"', env={'PATH': '/usr/bin:/bin'})
caso('scan: delega no instalado', decision(r), 'pasar')
r, _ = hook('delega-scan.py', 'git commit -m "arreglo para ZORBACORP"')
caso('scan: término en el mensaje', decision(r), 'bloquear')
caso('scan: no imprime el término', 'oculto' if 'orbacorp' not in r.stderr.lower() else 'visible', 'oculto')
open(os.path.join(repo, 'd.txt'), 'w').write('cliente Zorbacorp\n'); g('add', 'd.txt')
r, _ = hook('delega-scan.py', 'git commit -m "datos"')
caso('scan: término en lo preparado', decision(r), 'bloquear')
g('reset', '-q', 'd.txt'); os.remove(os.path.join(repo, 'd.txt'))
r, _ = hook('delega-scan.py', 'git add -A && git commit -m "limpio"')
caso('scan: commit limpio', decision(r), 'pasar')
r, _ = hook('delega-scan.py', 'git push origin delega/limpia')
caso('scan: push de rama limpia', decision(r), 'pasar')
r, _ = hook('delega-scan.py', 'git push -u origin feature/sucia')
caso('scan: push de rama con el término', decision(r), 'bloquear')
r, _ = hook('delega-scan.py', f'cd {repo} && gh pr create --title x --body "para zorbacorp"')
caso('scan: cuerpo de PR', decision(r), 'bloquear')
cuerpo = os.path.join(tmp, 'cuerpo.md'); open(cuerpo, 'w').write('Zorbacorp\n')
r, _ = hook('delega-scan.py', f'gh issue comment 3 --body-file {cuerpo}')
caso('scan: --body-file', decision(r), 'bloquear')

# delega-pr-gate
r, _ = hook('delega-pr-gate.py', 'gh pr create --head feature/sucia --title x')
caso('gate: rama que no es de trabajador', decision(r), 'pasar')
r, _ = hook('delega-pr-gate.py', 'gh pr create --title x --body y')
caso('gate: delega/* sin integrate', decision(r), 'bloquear')
caso('gate: dice el comando', 'sí' if f'delega integrate {os.path.realpath(repo)} delega/limpia' in r.stderr else 'no', 'sí')
r, _ = hook('delega-pr-gate.py', 'gh pr create --base develop --title x')
caso('gate: --base pasa a --into', 'sí' if '--into develop' in r.stderr else 'no', 'sí')
auditar(branch='delega/limpia', result='conflicts', sha=sha_limpia)
r, _ = hook('delega-pr-gate.py', 'gh pr create --title x')
caso('gate: integrate con conflictos', decision(r), 'bloquear')
auditar(branch='delega/limpia', result='verified', sha='0000000')
r, _ = hook('delega-pr-gate.py', 'gh pr create --title x')
caso('gate: integrate de otro sha', decision(r), 'bloquear')
auditar(branch='delega/limpia', result='verified', sha=sha_limpia)
r, _ = hook('delega-pr-gate.py', 'gh pr create --title x')
caso('gate: integrate verificado en el sha', decision(r), 'pasar')
r, _ = hook('delega-pr-gate.py', 'gh pr create --head orq/x --title x')
caso('gate: orq/* sin integrate', decision(r), 'bloquear')
r, _ = hook('delega-pr-gate.py', 'gh pr list')
caso('gate: otro gh', decision(r), 'pasar')

# modelo-orquestador
ev = lambda m: {'hook_event_name': 'SessionStart', 'source': 'startup', 'model': m, 'cwd': repo}
aviso = lambda r: 'avisa' if '[1m]' in r.stdout else 'calla'
r, _ = hook('modelo-orquestador.py', '', ev=ev('claude-opus-5-5[1m]'))
caso('modelo: [1m] en repo de un perfil', aviso(r), 'avisa')
r, _ = hook('modelo-orquestador.py', '', ev=ev('claude-opus-5-5'))
caso('modelo: sin [1m]', aviso(r), 'calla')
r, _ = hook('modelo-orquestador.py', '', ev=ev('opus[1m]'), env={'DELEGA_CHANNEL': '/x'})
caso('modelo: trabajador de delega', aviso(r), 'calla')
r, _ = hook('modelo-orquestador.py', '', ev=dict(ev('opus[1m]'), cwd=tmp))
caso('modelo: carpeta ajena', aviso(r), 'calla')

print('fallos:', fallos)
sys.exit(1 if fallos else 0)
