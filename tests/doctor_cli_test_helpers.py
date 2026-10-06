"""Run the real doctor CLI under the selected invoking host in a child process."""
import os
from pathlib import Path
import subprocess
import sys


def run_doctor(command, **kwargs):
    from runtime_context import current_runtime_context
    actor = current_runtime_context()
    assert actor is not None
    root = Path(__file__).resolve().parents[1]
    child = ('import sys; from runtime_context import resolve_runtime_context,using_runtime_context; '
             'from vault_doctor import main; '
             'ctx=resolve_runtime_context(sys.argv[1],sys.argv[2],{},'
             "{'config_path':sys.argv[3],'resource_root':sys.argv[4],'session_id':sys.argv[5],"
             "'cwd':sys.argv[6],'vault_path':sys.argv[7]}); "
             'sys.argv=["vault-doctor",*sys.argv[8:]]; '
             '\nwith using_runtime_context(ctx): raise SystemExit(main())')
    environment = dict(kwargs.pop('env', os.environ))
    environment['PYTHONPATH'] = os.pathsep.join([str(root / 'hooks'), str(root / 'scripts')])
    # Historical Claude fixtures explicitly select their storage root. This
    # does not select the actor: --host/client/config come from the fixture.
    if 'CLAUDE_CONFIG_DIR' not in environment or environment['HOME'] != os.environ.get('HOME'):
        environment['CLAUDE_CONFIG_DIR'] = str(Path(environment['HOME']) / '.claude')
    vault = (command[command.index('--vault') + 1] if '--vault' in command else
             environment.get('OBSIDIAN_BRAIN_VAULT', str(actor.vault_path)))
    timeout = kwargs.pop('timeout', 30)
    return subprocess.run([sys.executable, '-c', child, actor.host, actor.client,
                           str(actor.config_path), str(root), actor.native_session_id,
                           str(actor.canonical_project_root), vault, *command[2:]],
                          env=environment, stdin=subprocess.DEVNULL, timeout=timeout, **kwargs)
