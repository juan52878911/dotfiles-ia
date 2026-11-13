"""Casos del hook no-cat-grande con ficheros de prueba: grande por líneas, grande por bytes y pequeño."""
import json, os, subprocess, sys, tempfile

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'claude', 'hooks', 'no-cat-grande.py')
tmp = tempfile.mkdtemp(prefix='no-cat-grande-')
open(os.path.join(tmp, 'grande.txt'), 'w').write(''.join(f'{i}\n' for i in range(5000)))
open(os.path.join(tmp, 'mini.json'), 'w').write('x' * 200000)
open(os.path.join(tmp, 'poco.txt'), 'w').write('hola\n' * 10)


def corta(cmd, **extra):
    ev = {'cwd': tmp, 'tool_input': {'command': cmd}}
    r = subprocess.run([sys.executable, HOOK], input=json.dumps(ev), capture_output=True, text=True,
                       env=dict(os.environ, **extra))
    assert r.returncode == 0, r.stderr
    return bool(r.stdout) and json.loads(r.stdout)['hookSpecificOutput']['permissionDecision'] == 'deny'


casos = [
    ('cat de fichero grande', 'cat grande.txt', True),
    ('cat de JSON minificado grande', 'cat mini.json', True),
    ('cat de fichero pequeño', 'cat poco.txt', False),
    ('tubería a head', 'cat grande.txt | head -5', False),
    ('tubería a grep', 'cat grande.txt | grep 7', False),
    ('redirección a fichero', 'cat grande.txt > copia.txt', False),
    ('2>&1 no saca la salida', 'cat grande.txt 2>&1', True),
    ('sed con rango corto', "sed -n '1,100p' grande.txt", False),
    ('sed con rango largo', "sed -n '1,900p' grande.txt", True),
    ('head con muchas líneas', 'head -n 2000 grande.txt', True),
    ('heredoc', 'cat <<EOF\nhola\nEOF', False),
    ('dentro de $( )', 'echo $(cat grande.txt)', True),
    ('subshell', '(cat grande.txt)', True),
    ('cd y ruta relativa', f'cd / && cat {tmp}/grande.txt', True),
    ('fichero que no existe', 'cat no-existe.txt', False),
    ('desactivado', 'cat grande.txt', False, {'NO_CAT_GRANDE': '0'}),
]
fallos = 0
for nombre, cmd, esperado, *extra in casos:
    ok = corta(cmd, **(extra[0] if extra else {})) == esperado
    fallos += not ok
    print(('ok   ' if ok else 'FALLA'), nombre)
sys.exit(1 if fallos else 0)
