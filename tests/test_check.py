import sqlite3

from hearsay import check, conversations, enqueue
from hearsay.conversations import STOPPED, conversation_id
from test_assemble import T, add_conversations, audio_burst


def test_the_pipeline_is_enqueued_once_a_conversation_ends_that_the_database_has_open_or_lacks(tmp_path, monkeypatch):
    env = {name: tmp_path / name for name in check.DIRS if name != "db"}
    for d in env.values():
        d.mkdir()
    env["db"] = tmp_path / "db" / "hearsay.sqlite"
    env["db"].parent.mkdir()
    # The audio runs to T. One conversation ended 16 minutes before, the other is still going on.
    audio_burst(env["raw_dir"], 0, T)
    now = T.timestamp()
    ended, going_on = (now - 1000, now - 960), (now - 100, now - 60)

    def speech(raw_dir, bursts, cache=None):
        """What voice detection would find."""
        return [ended, going_on]

    posted = []

    def post(corral_url, corral_token, spec):
        posted.append(spec)
        return 1, True

    monkeypatch.setattr(conversations, "detect_speech", speech)
    monkeypatch.setattr(enqueue, "post", post)

    def run(at=now):
        posted.clear()
        check.run(env, "http://corral", "token", at)
        return posted

    # Before the first reprocess there's no database: it lacks the conversation that ended, and isn't made.
    assert run() == [enqueue.CHAIN]
    assert not env["db"].exists()

    # Reprocess made it while both were going on, so it has the one that ended as open.
    first, second = conversation_id(ended[0]), conversation_id(going_on[0])
    add_conversations(env["db"], [(first, ended[0] - 1, ended[1] + 1, 40.0, 1),
                                  (second, going_on[0] - 1, going_on[1] + 1, 40.0, 1)])
    assert run() == [enqueue.CHAIN]

    # Once the pipeline's reprocess has it as ended, nothing new has: nothing is enqueued.
    db = sqlite3.connect(env["db"])
    db.execute("UPDATE conversations SET open = 0 WHERE conversation_id = ?", (first,))
    db.commit()
    db.close()
    assert run() == []

    # The recording stopped right after the second's last speech, so it stays open. The database has it as
    # reprocess saw it earlier on: until STOPPED has passed it's still going on, then it has ended.
    db = sqlite3.connect(env["db"])
    db.execute("UPDATE conversations SET end = end - 30 WHERE conversation_id = ?", (second,))
    db.commit()
    db.close()
    assert run() == []
    assert run(now + STOPPED) == [enqueue.CHAIN]

    # Once the pipeline's reprocess has it as the check sees it, nothing is enqueued again.
    db = sqlite3.connect(env["db"])
    db.execute("UPDATE conversations SET end = end + 30 WHERE conversation_id = ?", (second,))
    db.commit()
    db.close()
    assert run(now + STOPPED) == []
