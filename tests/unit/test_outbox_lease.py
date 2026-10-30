import unittest

from app.state import Store


class OutboxLeaseTests(unittest.TestCase):
    def test_only_one_worker_can_reclaim_the_observed_expired_lease(self):
        store = Store()
        try:
            store.insert_outbox_intended("reply", None, "persona_reply", 1, {}, "2020-01-01T00:00:00+00:00")
            store.conn.commit()
            self.assertTrue(store.claim_outbox("reply", "worker-0", "2020-01-01T00:00:00+00:00"))
            store.conn.commit()

            observed_lease = store.get_outbox("reply")["claimed_at"]
            self.assertTrue(store.reclaim_outbox("reply", "worker-1", "2020-01-01T00:00:01+00:00",
                                                  expected_claimed_at=observed_lease))
            store.conn.commit()
            self.assertFalse(store.reclaim_outbox("reply", "worker-2", "2020-01-01T00:00:02+00:00",
                                                   expected_claimed_at=observed_lease))
        finally:
            store.conn.close()


if __name__ == "__main__":
    unittest.main()
