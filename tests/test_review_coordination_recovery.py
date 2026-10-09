"""Physical-vault locks and registered intent recovery use disposable state."""
import json
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from parity_test_helpers import host, selected_host_context, context, host_identity_scenario
import note_transactions as tx


def test_same_physical_vault_ignores_mutable_home(context, host_identity_scenario):
    other_home = context.user_home.parent / 'different-home'
    other_home.mkdir()
    other = replace(context, user_home=other_home)
    host_identity_scenario.register(context, other)
    observations = []
    def compete():
        try:
            with tx.ownership_lock(other):
                observations.append('entered')
        except tx.LockBusy:
            observations.append('busy')
    with tx.ownership_lock(context):
        thread = threading.Thread(target=compete)
        thread.start()
        thread.join(2)
    assert observations == ['busy']
    assert tx.coordination_location(context) == tx.coordination_location(other)


def test_vault_at_home_keeps_journal_outside_vault(context, host_identity_scenario):
    other = replace(context, user_home=context.vault_path)
    host_identity_scenario.register(context, other)
    location = tx.coordination_location(other)
    assert not location.resolve().is_relative_to(other.vault_path)
    with tx.ownership_lock(other):
        tx.connect_coordination(other).close()


def test_trusted_state_root_symlink_resolves_once(context, monkeypatch, tmp_path, host_identity_scenario):
    root = tmp_path / 'real-state'
    root.mkdir()
    link = tmp_path / 'state-link'
    link.symlink_to(root, target_is_directory=True)
    monkeypatch.setenv('XDG_STATE_HOME', str(link))
    other = replace(context, coordination_root=None)
    host_identity_scenario.register(context, other)
    assert other.coordination_root == root.resolve()
    before = tx.coordination_location(other)
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'different-state'))
    assert tx.coordination_location(other) == before
    tx.coordination_path(other)


def _legacy(context, value):
    folder = context.index_path.parent / ('.'+context.index_path.name+'.coordination')
    folder.mkdir(parents=True)
    path = folder / 'state.sqlite3'
    with sqlite3.connect(path) as connection:  # noqa: vault-db-connect — isolated old coordination fixture
        connection.executescript('CREATE TABLE identity(vault TEXT PRIMARY KEY); CREATE TABLE marker(value TEXT PRIMARY KEY);')
        connection.execute('INSERT INTO identity VALUES (?)',(str(context.vault_path),))
        connection.execute('INSERT INTO marker VALUES (?)',(value,))
    path.chmod(0o600)
    return path


def test_second_verified_index_journal_is_not_ignored(context, host_identity_scenario):
    other = replace(context, index_path=context.index_path.parent / 'second-index.sqlite')
    host_identity_scenario.register(context, other)
    first = _legacy(context, 'first')
    second = _legacy(other, 'second')
    originals = [first.read_bytes(),second.read_bytes()]
    for actor in (context,other,context):
        with tx.ownership_lock(actor):
            connection = tx.connect_coordination(actor)
            connection.close()
    with tx.ownership_lock(context):
        connection = tx.connect_coordination(context)
        rows = connection.execute('SELECT value FROM marker ORDER BY value').fetchall()
        connection.close()
    assert rows == [('first',),('second',)]
    assert [first.read_bytes(),second.read_bytes()] == originals


def test_locked_prior_journal_does_not_wait_past_hook_budget(context):
    import time
    prior = _legacy(context, 'preserved')
    original = prior.read_bytes()
    connection = sqlite3.connect(prior)  # noqa: vault-db-connect — isolated legacy lock fixture
    connection.execute('BEGIN EXCLUSIVE')
    started = time.monotonic()
    try:
        with tx.ownership_lock(context):
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                tx.connect_coordination(context)
        assert time.monotonic() - started < 1.0
    finally:
        connection.rollback()
        connection.close()
    assert prior.read_bytes() == original
    with tx.ownership_lock(context):
        current = tx.connect_coordination(context)
        assert current.execute('SELECT value FROM marker').fetchall() == [('preserved',)]
        current.close()


def test_recovery_discovers_other_registered_session(context, host_identity_scenario):
    other = replace(context, native_session_id='other-session', state_path=context.state_path.parent/'other-state')
    host_identity_scenario.register(context, other)
    note = context.vault_path / 'pending.md'
    note.write_text('manual')
    result = tx.apply_mutations(other,[tx.NoteMutation(note,'missing-revision',{'document':'requested'},'other-pending')])
    assert result.status == 'conflict'
    recovered = tx.recover_pending_mutations(context)
    assert recovered.status == 'pending'
    assert result.pending_path.exists()
    assert note.read_text() == 'manual'
    inventory = tx.pending_mutation_inventory(context)
    assert inventory['pending_mutations'] == 1


def test_pending_intent_size_bound_prevents_unrecoverable_write(context):
    note = context.vault_path / 'large.md'
    result = tx.apply_mutations(context,[tx.NoteMutation(note,None,{'document':'x'*(4*1024*1024)},'oversized-intent')])
    assert result.status in {'conflict','pending'}
    assert not note.exists()
    assert not list(tx.session_state_path(context).glob('pending/*.json'))


def test_explicit_digest_ack_discards_intent_only(context):
    import hashlib
    note = context.vault_path / 'manual.md'
    note.write_text('manual stays')
    result = tx.apply_mutations(context,[tx.NoteMutation(note,'wrong',{'document':'replacement'},'discard-conflict')])
    before = note.read_bytes()
    digest = hashlib.sha256(result.pending_path.read_bytes()).hexdigest()
    denied = tx.discard_pending_mutation(context,result.pending_path,'0'*64)
    assert denied.status == 'conflict' and result.pending_path.exists()
    acknowledged = tx.discard_pending_mutation(context,result.pending_path,digest)
    assert acknowledged.status == 'unchanged' and not result.pending_path.exists()
    assert note.read_bytes() == before


def test_admin_migration_budget_finishes_after_hook_budget_failure(context):
    import time
    old = _legacy(context,'large')
    with sqlite3.connect(old) as connection:  # noqa: vault-db-connect — isolated migration budget fixture
        connection.execute('CREATE TABLE retained(value BLOB)')
        connection.execute('INSERT INTO retained VALUES (zeroblob(8388608))')
    original = old.read_bytes()
    with tx.ownership_lock(context), tx.coordination_migration_budget(0.000001):
        with pytest.raises(tx.LockBusy,match='admin migration'):
            tx.connect_coordination(context)
    with tx.ownership_lock(context), tx.coordination_migration_budget(30):
        connection = tx.connect_coordination(context)
        assert connection.execute('SELECT length(value) FROM retained').fetchone() == (8388608,)
        connection.close()
    assert old.read_bytes() == original


def test_conflicting_migration_preserves_both_journals(context, host_identity_scenario):
    other = replace(context,index_path=context.index_path.parent/'collision.sqlite')
    host_identity_scenario.register(context,other)
    first = _legacy(context,'same')
    second = _legacy(other,'same')
    for path,value in [(first,'first'),(second,'second')]:
        with sqlite3.connect(path) as connection:  # noqa: vault-db-connect — isolated collision fixture
            connection.execute('CREATE TABLE keyed(id TEXT PRIMARY KEY,value TEXT)')
            connection.execute('INSERT INTO keyed VALUES (?,?)',('identity',value))
    originals = [first.read_bytes(),second.read_bytes()]
    with tx.ownership_lock(context):
        tx.connect_coordination(context).close()
    with tx.ownership_lock(other):
        with pytest.raises(ValueError,match='both journals were preserved'):
            tx.connect_coordination(other)
    with tx.ownership_lock(context):
        connection = tx.connect_coordination(context)
        assert connection.execute('SELECT value FROM keyed').fetchone() == ('first',)
        connection.close()
    assert [first.read_bytes(),second.read_bytes()] == originals


def test_pending_ack_checks_final_bytes_and_does_not_cancel_new_intent(context,monkeypatch):
    import hashlib
    note = context.vault_path/'ack-race.md'
    note.write_text('manual')
    mutation = tx.NoteMutation(note,'wrong',{'document':'generated'},'ack-race')
    result = tx.apply_mutations(context,[mutation])
    raw = result.pending_path.read_bytes()
    changed = raw+b' '
    def edit(point):
        if point == 'before_pending_discard':
            result.pending_path.write_bytes(changed)
    monkeypatch.setattr(tx,'_fault',edit)
    denied = tx.discard_pending_mutation(context,result.pending_path,hashlib.sha256(raw).hexdigest())
    assert denied.status == 'conflict'
    assert result.pending_path.read_bytes() == changed
    assert note.read_text() == 'manual'


def test_acknowledged_original_operation_cannot_recreate_pending(context):
    import hashlib
    note = context.vault_path/'ack.md'
    note.write_text('manual')
    mutation = tx.NoteMutation(note,'wrong',{'document':'generated'},'ack-original')
    result = tx.apply_mutations(context,[mutation])
    digest = hashlib.sha256(result.pending_path.read_bytes()).hexdigest()
    assert tx.discard_pending_mutation(context,result.pending_path,digest).status == 'unchanged'
    retried = tx.apply_mutations(context,[mutation])
    assert retried.status == 'conflict' and retried.pending_path is None
    assert not list(result.pending_path.parent.glob('*.json'))
    assert note.read_text() == 'manual'


def test_child_symlink_under_frozen_coordination_root_is_rejected(context,tmp_path):
    outside = tmp_path/'outside'
    outside.mkdir()
    root = context.coordination_root
    (root/'obsidian-brain').symlink_to(outside,target_is_directory=True)
    with pytest.raises(ValueError,match='symbolic links'):
        tx.coordination_path(context)
    assert not list(outside.iterdir())


def test_inventory_distinguishes_unregistered_selected_directory(context):
    pending = tx._private_dir(tx.session_state_path(context)/'pending')
    (pending/'legacy.json').write_text('{}')
    value = tx.pending_mutation_inventory(context)
    assert value['pending_mutations'] == 0 and value['selected_pending_registered'] is False


def test_registered_other_session_conflicts_cannot_starve_later_intent(context,host_identity_scenario):
    actors = []
    notes = []
    for number in range(10):
        actor = replace(context,native_session_id='fair-session-%d'%number,state_path=context.state_path.parent/('actor-%d'%number))
        host_identity_scenario.register(actor)
        actors.append(actor)
        note = context.vault_path/('fair-%d.md'%number)
        note.write_text('original')
        revision = tx.read_revision(actor,note)
        tx._pending(actor,'fair-%d'%number,json.dumps({'path':str(note),'expected':revision,
            'changes':{'document':'recovered'},'operation':'fair-%d'%number}))
        notes.append(note)
    for note in notes[:9]:
        note.write_text('manual')
    for _ in range(20):
        tx.recover_pending_mutations(context,max_operations=2)
        if notes[-1].read_text() == 'recovered':
            break
    assert notes[-1].read_text() == 'recovered'
    assert all(note.read_text() == 'manual' for note in notes[:9])
    assert tx.pending_mutation_inventory(context)['pending_mutations'] == 9


@pytest.mark.parametrize('same_bytes',[True,False])
def test_concurrent_intent_replacement_survives_recovery_unlink(context,monkeypatch,same_bytes):
    note = context.vault_path/'replace-intent.md'
    note.write_text('original')
    raw = json.dumps({'path':str(note),'expected':tx.read_revision(context,note),
        'changes':{'document':'recovered'},'operation':'replace-intent'})
    intent = tx._pending(context,'replace-intent',raw)
    replacement = raw if same_bytes else raw+' '
    fired = []
    def replace_intent(point):
        if point == 'before_pending_recovery_unlink' and not fired:
            tx._atomic_write(intent,replacement)
            fired.append(True)
    monkeypatch.setattr(tx,'_fault',replace_intent)
    result = tx.recover_pending_mutations(context)
    assert fired and result.status == 'pending'
    assert intent.read_text() == replacement
    assert note.read_text() == 'recovered'


def test_journal_namespace_uses_physical_directory_identity(context):
    import hashlib
    info = context.vault_path.stat()
    expected = hashlib.sha256(('%d:%d'%(info.st_dev,info.st_ino)).encode()).hexdigest()
    assert tx.coordination_location(context).name == expected
    with tx.ownership_lock(context):
        connection = tx.connect_coordination(context)
        assert tx.coordination_identity_matches(context,connection)
        connection.close()


def test_default_account_state_ignores_HOME_without_touching_it(context,monkeypatch,tmp_path):
    import pwd
    from types import SimpleNamespace
    account = tmp_path/'mock-account'
    account.mkdir()
    monkeypatch.delenv('XDG_STATE_HOME',raising=False)
    monkeypatch.setenv('HOME',str(tmp_path/'mutable-home'))
    monkeypatch.setattr(pwd,'getpwuid',lambda uid:SimpleNamespace(pw_dir=str(account)))
    other = replace(context,coordination_root=None)
    assert other.coordination_root == account/'.local'/'state'
    assert not other.coordination_root.exists()


def test_account_home_vault_chooses_outside_fallback_without_creating_it(context,monkeypatch):
    import pwd
    from types import SimpleNamespace
    monkeypatch.delenv('XDG_STATE_HOME',raising=False)
    monkeypatch.setattr(pwd,'getpwuid',lambda uid:SimpleNamespace(pw_dir=str(context.vault_path)))
    other = replace(context,coordination_root=None)
    assert not other.coordination_root.is_relative_to(context.vault_path)
    assert str(other.coordination_root).startswith('/private/var/tmp/obsidian-brain-state-') or str(other.coordination_root).startswith('/var/tmp/obsidian-brain-state-')
    # Deliberately perform no IO in the system fallback: this is selection only.


def test_relative_XDG_state_is_rejected_before_any_state_write(context,monkeypatch):
    monkeypatch.setenv('XDG_STATE_HOME','relative-state')
    with pytest.raises(ValueError,match='must be absolute'):
        replace(context,coordination_root=None)


@pytest.mark.parametrize('root_kind',['account','fallback'])
def test_test_guard_refuses_live_coordination_before_creating_state(context,monkeypatch,root_kind):
    import conftest
    selected = (conftest._ACCOUNT_COORDINATION_ROOT if root_kind=='account'
                else conftest._SYSTEM_COORDINATION_ROOT)
    other=replace(context,coordination_root=selected)
    touched=[]
    original=Path.mkdir
    def observed(path,*args,**kwargs):
        if path==selected or path.is_relative_to(selected):
            touched.append(path)
            raise AssertionError('guard reached forbidden write')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'mkdir',observed)
    with pytest.raises(AssertionError,match='actual account or UID fallback'):
        tx.coordination_path(other)
    assert not touched


def test_child_environment_cannot_drop_private_coordination_state(context,monkeypatch):
    import os
    import subprocess
    import sys
    environment=dict(os.environ);environment.pop('XDG_STATE_HOME',None)
    with pytest.raises(AssertionError,match='explicit private XDG_STATE_HOME'):
        subprocess.run([sys.executable,'-c','raise SystemExit(99)'],env=environment,
                       stdin=subprocess.DEVNULL,capture_output=True,timeout=5)


def test_physical_case_alias_cannot_place_state_inside_vault(context,monkeypatch,host_identity_scenario):
    import os
    alias=context.vault_path.with_name(context.vault_path.name.upper())
    assert alias != context.vault_path
    original=Path.stat
    # Model the same native inode on case-sensitive CI; macOS uses the real alias.
    if not alias.exists():
        def aliased_stat(path,*args,**kwargs):
            if path==alias or path.is_relative_to(alias):
                path=context.vault_path/path.relative_to(alias)
            return original(path,*args,**kwargs)
        monkeypatch.setattr(Path,'stat',aliased_stat)
    private_inside=context.vault_path/'.local'/'state'
    monkeypatch.setenv('XDG_STATE_HOME',str(private_inside))
    selected=replace(context,vault_path=alias,coordination_root=None)
    assert selected.coordination_root != private_inside
    assert not private_inside.exists()
    explicit=replace(context,vault_path=alias,coordination_root=private_inside)
    host_identity_scenario.register(context,explicit)
    with pytest.raises(ValueError,match='outside the vault'):
        tx.coordination_location(explicit)
    assert not private_inside.exists()


def test_utf8_intent_size_does_not_charge_json_escape_expansion(context):
    body='漢'*(1024*1024)
    note=context.vault_path/'unicode-large.md'
    result=tx.apply_mutations(context,[tx.NoteMutation(note,None,{'document':body},'unicode-large')])
    assert result.status=='applied' and note.read_bytes()==body.encode('utf-8')
    repeated=tx.apply_mutations(context,[tx.NoteMutation(note,None,{'document':body},'unicode-large')])
    assert repeated.status=='unchanged'


def test_unicode_conflict_retains_bounded_utf8_intent_and_manual_note(context):
    note=context.vault_path/'unicode-pending.md';note.write_text('manual body')
    body='😀'*700000
    result=tx.apply_mutations(context,[tx.NoteMutation(note,'stale-source',{'document':body},'unicode-pending')])
    assert result.status=='conflict' and result.pending_path.is_file()
    raw=result.pending_path.read_bytes()
    assert len(raw)<tx.MAX_PENDING_BYTES and body in raw.decode('utf-8')
    assert note.read_text()=='manual body'


def test_readonly_physical_identity_rejects_another_vault(context,tmp_path):
    other=tmp_path/'another-vault';other.mkdir()
    details=other.stat();identity=f'{details.st_dev}:{details.st_ino}'
    with tx.ownership_lock(context):
        connection=tx.connect_coordination(context)
        connection.execute('UPDATE physical_identity SET vault_id=?',(identity,));connection.commit()
        assert tx.coordination_identity_matches(context,connection) is False
        assert connection.execute('SELECT vault_id FROM physical_identity').fetchone()==(identity,)
        connection.close()
