"""Retained CAS conflicts must not starve later recoverable mutations."""
import time
import json

from parity_test_helpers import host, selected_host_context, context
import note_transactions as tx


def test_conflicting_batch_does_not_starve_ninth_intent(context):
    paths = []
    for number in range(9):
        path = context.vault_path / ("pending-%d.md" % number)
        path.write_text("original\n", encoding="utf-8")
        revision = tx.read_revision(context, path)
        tx._pending(context, "fairness-%d" % number, json.dumps({
            "path": str(path), "expected": revision,
            "changes": {"document": "recovered\n"},
            "operation": "fairness-%d" % number,
        }))
        paths.append(path)
    for path in paths[:8]:
        path.write_text("user edit\n", encoding="utf-8")
    for _ in range(5):
        tx.recover_pending_mutations(context, max_operations=8,
                                     deadline=time.monotonic() + 2)
        if "recovered" in paths[8].read_text(encoding="utf-8"):
            break
    assert "recovered" in paths[8].read_text(encoding="utf-8")
    assert all(path.read_text(encoding="utf-8") == "user edit\n"
               for path in paths[:8])
    pending = tx.session_state_path(context) / "pending"
    assert len(list(pending.glob("*.json"))) == 8


def test_recovery_cursor_symlink_does_not_read_external_state(context, tmp_path):
    pending = tx._private_dir(tx.session_state_path(context) / "pending")
    outside = tmp_path / "outside-cursor"
    outside.write_text('{"after":"' + "f" * 64 + '.json"}', encoding="utf-8")
    (pending / ".recovery-cursor").symlink_to(outside)
    assert tx.recover_pending_mutations(context).status == "unchanged"
    assert outside.read_text(encoding="utf-8") == '{"after":"' + "f" * 64 + '.json"}'


def test_real_lockbusy_batch_conflicts_keep_one_original_intent(context):
    import threading
    paths = [context.vault_path / ('batch-%d.md' % number) for number in range(2)]
    mutations = []
    for number, path in enumerate(paths):
        path.write_text('original\n')
        mutations.append(tx.NoteMutation(path, tx.read_revision(context, path),
                                        {'document': 'generated\n'}, 'grouped-%d' % number))
    entered, release = threading.Event(), threading.Event()
    def hold_writer():
        with tx.ownership_lock(context):
            entered.set()
            assert release.wait(timeout=2)
    thread = threading.Thread(target=hold_writer)
    thread.start()
    try:
        assert entered.wait(timeout=1)
        deferred = tx.apply_mutations(context, mutations)
        assert deferred.status == 'pending'
    finally:
        release.set()
        thread.join(timeout=2)
    assert not thread.is_alive()
    original_intent = deferred.pending_path
    original_bytes = original_intent.read_bytes()
    for path in paths:
        path.write_text('manual edit\n')
    for _ in range(3):
        result = tx.recover_pending_mutations(context, max_operations=8,
                                             deadline=time.monotonic() + 1)
        assert result.status == 'pending'
        assert original_intent.read_bytes() == original_bytes
        assert list(original_intent.parent.glob('*.json')) == [original_intent]
        assert all(path.read_text() == 'manual edit\n' for path in paths)
