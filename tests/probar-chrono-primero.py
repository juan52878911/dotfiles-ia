"""Casos del hook chrono-primero con un repo de prueba, un índice vacío y un `chrono` falso en PATH."""
import json, os, subprocess, sys, tempfile

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'claude', 'hooks', 'chrono-primero.py')
tmp = tempfile.mkdtemp(prefix='chrono-primero-')
repo, sin, binario = (os.path.join(tmp, n) for n in ('repo', 'sin', 'bin'))
for d in (os.path.join(repo, '.chrono'), sin, binario):
    os.makedirs(d)
open(os.path.join(repo, '.chrono', 'index.db'), 'w').close()
open(os.path.join(repo, 'a.py'), 'w').write('x = 1\n')
with open(os.path.join(binario, 'chrono'), 'w') as f:
    f.write('#!/bin/sh\nexit 0\n')
os.chmod(os.path.join(binario, 'chrono'), 0o755)
entorno = dict(os.environ, PATH=binario + os.pathsep + os.environ['PATH'], XDG_CACHE_HOME=os.path.join(tmp, 'cache'),
               HOME=tmp)


def corta(cmd, cwd=repo, sesion='s', **extra):
    ev = {'session_id': sesion, 'cwd': cwd, 'tool_input': {'command': cmd}}
    r = subprocess.run([sys.executable, HOOK], input=json.dumps(ev), capture_output=True, text=True,
                       env=dict(entorno, **extra))
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)['hookSpecificOutput']['permissionDecisionReason'] if r.stdout else None


fallos = 0
def caso(nombre, obtenido, esperado):
    global fallos
    ok = obtenido == esperado if isinstance(esperado, bool) else esperado in (obtenido or '')
    fallos += not ok
    print(('ok   ' if ok else 'FALLA'), nombre)

caso('blame con rango propone explain con rango', corta('git blame -L 3,9 a.py'), 'chrono explain a.py:3-9')
caso('repetir la misma orden pasa', corta('git blame -L 3,9 a.py') is None, True)
caso('otra sesión vuelve a cortar', corta('git blame -L 3,9 a.py', sesion='t') is not None, True)
caso('log -L', corta('git log -L10,20:a.py -s'), 'chrono explain a.py:10-20')
caso('log -- ruta', corta('git status && git log --oneline -5 -- a.py | head'), 'chrono explain a.py')
caso('log -p ruta', corta('git -C . log -p a.py'), 'chrono explain a.py')
caso('log -1 -- ruta pasa', corta('git log --oneline -1 -- a.py') is None, True)
caso('rango de ramas pasa', corta('git log --oneline origin/dev..HEAD') is None, True)
caso('orden que ya usa chrono pasa', corta('chrono explain a.py; git blame a.py') is None, True)
caso('sin índice pasa', corta('git blame a.py', cwd=sin) is None, True)
caso('desactivado pasa', corta('git blame a.py', sesion='u', CHRONO_PRIMERO='0') is None, True)
caso('sin chrono pasa', corta('git blame a.py', sesion='v', PATH='/usr/bin:/bin') is None, True)
sys.exit(1 if fallos else 0)
