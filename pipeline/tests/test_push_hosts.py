"""Browser alerts go only to browsers' push services: python tests/test_push_hosts.py (offline, SQLite).

push_subscriptions takes inserts from anyone with the site's public key. A row whose endpoint is not
a push service would have the pipeline post to an address of the sender's choosing and wait on it,
so it is deleted unsent."""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, insert, select  # noqa: E402

from digest import config, db, push  # noqa: E402

REAL = [
    "https://fcm.googleapis.com/fcm/send/dXk2:APA91bH",
    "https://updates.push.services.mozilla.com/wpush/v2/gAAAAABm",
    "https://wns2-par02p.notify.windows.com/w/?token=BQYAAAB",
    "https://web.push.apple.com/QGuQyavXutnMH",
    "https://FCM.googleapis.com:443/fcm/send/x",
]
FAKE = [
    "https://evil.example/collect",
    "https://fcm.googleapis.com.evil.example/x",
    "https://notfcm.googleapis.com/x",
    "https://storage.googleapis.com/bucket/x",
    "http://fcm.googleapis.com/fcm/send/x",
    "https://fcm.googleapis.com:8443/x",
    "https://user:pw@fcm.googleapis.com/x",
    "https://169.254.169.254/latest/meta-data",
    "https://[::1]/x",
    "javascript:alert(1)",
    "",
    None,
]


def test_only_push_services_pass():
    for url in REAL:
        assert push.push_service_ok(url), url
    for url in FAKE:
        assert not push.push_service_ok(url), url


def test_a_made_up_endpoint_is_deleted_unsent():
    eng = create_engine("sqlite://", future=True)
    db.metadata.create_all(eng)
    now = db.utcnow()
    with eng.begin() as conn:
        conn.execute(insert(db.stories).values(
            id=1, slug="big-news", headline="Big news", status="published", importance=9, score=10.0,
            article_count=3, key_points=["It happened."], summary_md="It happened.",
            first_published_at=now - timedelta(minutes=30), updated_at=now))
        for i, url in enumerate(["https://fcm.googleapis.com/fcm/send/real", "https://evil.example/slow"], 1):
            conn.execute(insert(db.push_subscriptions).values(
                id=i, endpoint=url, p256dh="k" * 87, auth="a" * 22, failures=0, created_at=now))
    sent_to = []

    class FakeWebPush:  # stands in for pywebpush: records where it would post
        class WebPushException(Exception):
            response = None

        @staticmethod
        def webpush(subscription_info, **_):
            sent_to.append(subscription_info["endpoint"])

    saved = (db._engine, config.VAPID_PRIVATE_KEY, config.PUBLIC_VAPID_KEY, sys.modules.get("pywebpush"))
    db._engine, config.VAPID_PRIVATE_KEY, config.PUBLIC_VAPID_KEY = eng, "private", "public"
    sys.modules["pywebpush"] = FakeWebPush
    try:
        stats = push.run()
    finally:
        db._engine, config.VAPID_PRIVATE_KEY, config.PUBLIC_VAPID_KEY = saved[:3]
        if saved[3] is None:
            sys.modules.pop("pywebpush", None)
        else:
            sys.modules["pywebpush"] = saved[3]
    assert sent_to == ["https://fcm.googleapis.com/fcm/send/real"], sent_to
    assert stats["sent"] == 1 and stats["pruned"] == 1, stats
    with eng.connect() as conn:
        left = [r.endpoint for r in conn.execute(select(db.push_subscriptions)).all()]
    assert left == ["https://fcm.googleapis.com/fcm/send/real"], left


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all passed")
